import http.server
import json
import multiprocessing
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import github_admission as admission
import github_graphql_transport as transport

POLICY = admission.Policy(60, 20, 100, 10)
QUERY = 'query { viewer { login } rateLimit { cost } }'
TOKEN = 'test-secret-do-not-expose'
GOOD = {'data': {'viewer': {'login': 'public-user'}, 'rateLimit': {'cost': 2}}}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_POST(self):
        length = int(self.headers['Content-Length'])
        data = self.rfile.read(length)
        self.server.calls.append((self.path, self.headers.get('Authorization'), json.loads(data)))
        mode = self.server.mode
        if mode == 'slow': time.sleep(1)
        status = {'redirect': 302, '429': 429, '403': 403}.get(mode, 200)
        body = GOOD
        headers = {}
        if mode == 'redirect': headers['Location'] = '/redirected'; body = {'message': 'redirect'}
        if mode == '429': headers['Retry-After'] = '120'; body = {'message': 'limited'}
        if mode == '403': headers['Retry-After'] = '120'; body = {'data': {'rateLimit': {'cost': 1}}}
        if mode == 'errors': body = {'data': {'rateLimit': {'cost': 1}}, 'errors': [{'type': 'RATE_LIMITED', 'message': 'secondary rate limit'}]}
        if mode == 'secret': body = {'data': {'rateLimit': {'cost': 1}, TOKEN: 'echo ' + TOKEN}}
        if mode == 'large': body = {'payload': 'x' * 10000}
        raw = json.dumps(body).encode()
        try:
            self.send_response(status)
            for key, value in headers.items(): self.send_header(key, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ssl.SSLError, ConnectionResetError): pass
    def log_message(self, *args): pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, cert, key):
        super().__init__(address, Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.calls, self.mode = [], ''


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode('ascii').strip()
        self.server.calls.append(line)
        while self.rfile.readline().strip(): pass
        if not line.startswith('CONNECT '): return
        target = socket.create_connection(('127.0.0.1', self.server.target))
        try:
            self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); self.wfile.flush()
            import select
            sockets = [self.connection, target]
            while True:
                ready, _, _ = select.select(sockets, [], [], 2)
                if not ready: break
                for s in ready:
                    chunk = s.recv(65536)
                    if not chunk: return
                    (target if s is self.connection else self.connection).sendall(chunk)
        finally: target.close()


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def worker(state, port, cert, work, queue):
    os.chdir(work)
    try:
        env = {'GH_ENTERPRISE_TOKEN': TOKEN, 'SSL_CERT_FILE': cert, 'no_proxy': '*', 'PATH': os.environ['PATH']}
        gate = admission.GitHubAdmission(state, 'localhost', POLICY, lock_timeout=3)
        client = transport.GitHubGraphQL(gate, 'localhost', env=env, endpoint=f'https://localhost:{port}/api/graphql')
        queue.put(('ok', client.request(QUERY).cost))
    except Exception as exc: queue.put((type(exc).__name__, str(exc)))


class TransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-keyout', str(key), '-out', str(cls.cert), '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        cls.server = Server(('127.0.0.1', 0), str(cls.cert), str(key))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.port = cls.server.server_port
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()
    def setUp(self):
        self.tmpcase = tempfile.TemporaryDirectory(); self.addCleanup(self.tmpcase.cleanup)
        self.root = Path(self.tmpcase.name)
        self.server.calls.clear(); self.server.mode = ''
        self.now = 1000
        self.gate = admission.GitHubAdmission(self.root / 'state', 'localhost', POLICY, clock=lambda: self.now)
        self.env = {'GH_ENTERPRISE_TOKEN': TOKEN, 'SSL_CERT_FILE': str(self.cert), 'no_proxy': '*', 'PATH': os.environ['PATH']}
        self.client = self.make()
    def make(self, **kwargs):
        return transport.GitHubGraphQL(self.gate, 'localhost', env=self.env, endpoint=f'https://localhost:{self.port}/api/graphql', **kwargs)
    def test_success_and_exact_request(self):
        result = self.client.request(QUERY)
        self.assertEqual((result.status, result.cost, result.body), (200, 2, GOOD))
        self.assertFalse(result.admission['stale'])
        self.assertEqual(self.server.calls, [('/api/graphql', 'Bearer ' + TOKEN, {'query': QUERY})])
        self.assertNotIn(TOKEN, self.gate.path.read_text())
    def test_http_worker_ignores_workspace_python_startup_hooks(self):
        marker = self.root / 'leaked'
        (self.root / 'sitecustomize.py').write_text(
            f'import sys\nopen({str(marker)!r}, "wb").write(sys.stdin.buffer.read())\n')
        self.env['PYTHONPATH'] = str(self.root)
        self.assertEqual(self.make().request(QUERY).cost, 2)
        self.assertFalse(marker.exists())

    def test_http_worker_ignores_virtualenv_site_hooks(self):
        venv = self.root / 'venv'
        subprocess.run([sys.executable, '-I', '-m', 'venv', '--without-pip', str(venv)], check=True)
        interpreter = venv / 'bin' / 'python'
        site_dir = Path(subprocess.check_output([str(interpreter), '-I', '-c',
                         'import site; print(site.getsitepackages()[0])'], text=True).strip())
        marker = self.root / 'leaked-by-pth'
        (site_dir / 'hostile.pth').write_text(
            f'import sys; open({str(marker)!r}, "wb").write(sys.stdin.buffer.read())\n')
        with mock.patch.object(transport.sys, 'executable', str(interpreter)):
            self.assertEqual(self.make().request(QUERY).cost, 2)
        self.assertFalse(marker.exists())

    def test_redirect_not_followed_and_stale(self):
        self.server.mode = 'redirect'
        with self.assertRaises(admission.StateError): self.client.request(QUERY)
        self.assertEqual(len(self.server.calls), 1)
        self.assertTrue(self.client.status()['stale'])
    def test_graphql_errors_and_cooldown(self):
        self.server.mode = 'errors'
        result = self.client.request(QUERY)
        self.assertTrue(result.body['errors']); self.assertEqual(result.admission['cooldown_until'], 1060)
        with self.assertRaises(admission.CooldownActive): self.client.request(QUERY)
        self.assertEqual(len(self.server.calls), 1)
    def test_429_preserves_cooldown_with_unknown_cost(self):
        self.server.mode = '429'
        with self.assertRaises(admission.StateError): self.client.request(QUERY)
        state = self.client.status()
        self.assertTrue(state['stale']); self.assertEqual(state['cooldown_until'], 1120)
    def test_http_error_with_cost_commits_cooldown(self):
        self.server.mode = '403'
        with self.assertRaises(transport.TransportError) as e: self.client.request(QUERY)
        self.assertEqual(e.exception.status, 403); self.assertEqual(self.client.status()['cooldown_until'], 1120)
    def test_timeout_and_output_bound_mark_stale(self):
        self.server.mode = 'slow'
        with self.assertRaises(transport.TransportError) as e: self.make(timeout=.2).request(QUERY)
        self.assertEqual(e.exception.kind, 'timeout'); self.assertTrue(self.client.status()['stale'])
        gate = admission.GitHubAdmission(self.root / 'other', 'localhost', POLICY)
        self.server.mode = 'large'
        client = transport.GitHubGraphQL(gate, 'localhost', env=self.env, endpoint=f'https://localhost:{self.port}/api/graphql', max_output=1024)
        with self.assertRaises(transport.TransportError): client.request(QUERY)
        self.assertTrue(client.status()['stale'])
    def test_http_worker_is_reaped_after_deadline(self):
        self.server.mode = 'slow'
        real_popen = subprocess.Popen
        spawned = []
        def record(*args, **kwargs):
            process = real_popen(*args, **kwargs); spawned.append(process); return process
        with mock.patch.object(transport.subprocess, 'Popen', side_effect=record):
            with self.assertRaises(transport.TransportError): self.make(timeout=.2).request(QUERY)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        with self.assertRaises(ProcessLookupError): os.kill(spawned[0].pid, 0)
        self.assertEqual(len(self.server.calls), 1)

    def test_enterprise_cloud_endpoint_and_token_precedence(self):
        gate = admission.GitHubAdmission(self.root / 'cloud', 'tenant.ghe.com', POLICY)
        client = transport.GitHubGraphQL(gate, 'tenant.ghe.com', env={'GH_TOKEN': TOKEN, 'GITHUB_TOKEN': 'second'})
        self.assertEqual(client.endpoint, 'https://api.tenant.ghe.com/graphql')
        self.assertEqual(client._token(time.monotonic() + 1), TOKEN)

    def test_token_redaction_in_body_and_auth_failure(self):
        self.server.mode = 'secret'
        result = self.client.request(QUERY)
        self.assertNotIn(TOKEN, repr(result)); self.assertNotIn(TOKEN, self.gate.path.read_text())
        fake = self.root / 'gh'
        fake.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write({TOKEN!r})\nsys.exit(1)\n'); fake.chmod(0o700)
        self.env.pop('GH_ENTERPRISE_TOKEN')
        with self.assertRaises(transport.TransportError) as e: self.make(executable=str(fake)).request(QUERY)
        self.assertNotIn(TOKEN, str(e.exception))
    def test_auth_precedence_and_host_lookup(self):
        fake = self.root / 'gh'; calls = self.root / 'args'
        fake.write_text(f'#!{sys.executable}\nimport sys\nopen({str(calls)!r}, "w").write(repr(sys.argv[1:]))\nprint({TOKEN!r})\n'); fake.chmod(0o700)
        self.env.pop('GH_ENTERPRISE_TOKEN'); self.env['GH_TOKEN'] = 'wrong-host-token'
        self.assertEqual(self.make(executable=str(fake)).request(QUERY).cost, 2)
        self.assertEqual(self.server.calls[0][1], 'Bearer ' + TOKEN)
        self.assertEqual(calls.read_text(), "['auth', 'token', '--hostname', 'localhost', '--no-refresh']")
    def _auth_timeout_after_child_starts(self, *, parent_exits):
        import select
        from types import SimpleNamespace

        fake, fifo = self.root / 'gh', self.root / 'pid-fifo'
        os.mkfifo(fifo)
        child_code = 'import sys,time\nwhile True:\n sys.stderr.write("x"); sys.stderr.flush(); time.sleep(.05)'
        tail = f'print({TOKEN!r}, flush=True)\n' if parent_exits else 'import time\ntime.sleep(30)\n'
        fake.write_text(f'#!{sys.executable}\nimport subprocess, sys\np=subprocess.Popen([sys.executable, "-c", {child_code!r}])\nopen({str(fifo)!r}, "w").write(str(p.pid))\n' + tail)
        fake.chmod(0o700)
        self.env.pop('GH_ENTERPRISE_TOKEN')
        clock_offset = [0.0]
        clock = SimpleNamespace(monotonic=lambda: time.monotonic() + clock_offset[0])
        outcome = []
        def run():
            try: self.make(executable=str(fake), timeout=30).request(QUERY)
            except Exception as exc: outcome.append(exc)
        fifo_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        real_popen = subprocess.Popen
        spawned = []
        def record(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc
        try:
            with mock.patch.object(transport, 'time', clock), mock.patch.object(transport.subprocess, 'Popen', side_effect=record):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                try:
                    self.assertTrue(select.select([fifo_fd], [], [], 10)[0], 'fake gh did not start its child')
                    child = int(os.read(fifo_fd, 100))
                    if parent_exits:
                        self.assertEqual(spawned[0].wait(timeout=5), 0, 'fake auth parent did not exit')
                finally:
                    clock_offset[0] = 60
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'timed-out auth did not finish')
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], transport.TransportError)
            self.assertEqual(outcome[0].kind, 'timeout')
            for _ in range(100):
                try:
                    os.kill(child, 0)
                    stat = Path(f'/proc/{child}/stat')
                    if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z': break
                except ProcessLookupError: break
                time.sleep(.01)
            else: self.fail('auth descendant survived')
            self.assertFalse(self.gate.path.exists())
        finally:
            os.close(fifo_fd)

    def test_auth_timeout_kills_process_group(self):
        self._auth_timeout_after_child_starts(parent_exits=False)

    def test_deadline_includes_waiting_for_admission_lock(self):
        entered = threading.Event(); release = threading.Event()
        def holder():
            with self.gate._locked():
                entered.set(); release.wait(3)
        thread = threading.Thread(target=holder); thread.start(); self.assertTrue(entered.wait(2))
        try:
            start = time.monotonic()
            with self.assertRaises(transport.TransportError) as e: self.make(timeout=.2).request(QUERY)
            self.assertEqual(e.exception.kind, 'timeout')
            self.assertLess(time.monotonic() - start, 1)
            self.assertEqual(self.server.calls, [])
        finally:
            release.set(); thread.join(3)
        self.assertFalse(self.client.status()['stale'])

    def test_auth_parent_exit_with_pipe_held_kills_descendant(self):
        self._auth_timeout_after_child_starts(parent_exits=True)
        self.assertEqual(self.server.calls, [])

    def test_proxy_connect_and_enterprise_path(self):
        proxy = Proxy(('127.0.0.1', 0), ProxyHandler); proxy.target = self.port; proxy.calls = []
        thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
        try:
            self.env['https_proxy'] = f'http://127.0.0.1:{proxy.server_address[1]}'
            self.env['no_proxy'] = ''
            self.assertEqual(self.make().request(QUERY).cost, 2)
            self.assertEqual(len(proxy.calls), 1); self.assertTrue(proxy.calls[0].startswith(f'CONNECT localhost:{self.port} HTTP/1.'))
            self.assertEqual(self.server.calls[0][0], '/api/graphql')
        finally: proxy.shutdown(); proxy.server_close(); thread.join()
    def test_concurrent_workspaces_share_budget(self):
        state = str(self.root / 'shared'); works = [self.root / 'a', self.root / 'b']
        for p in works: p.mkdir()
        queue = multiprocessing.Queue()
        ps = [multiprocessing.Process(target=worker, args=(state, self.port, str(self.cert), str(p), queue)) for p in works]
        for p in ps: p.start()
        for p in ps: p.join(8); self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted(queue.get(timeout=1) for _ in ps), [('ok', 2), ('ok', 2)])
        gate = admission.GitHubAdmission(state, 'localhost', POLICY)
        self.assertEqual(sum(e[1] for e in json.loads(gate.path.read_text())['events']), 4)
    def test_invalid_endpoint_and_unavailable_state(self):
        with self.assertRaises(ValueError): transport.GitHubGraphQL(self.gate, 'localhost', endpoint='https://evil.example/api/graphql')
        state = self.root / 'file'; state.write_text('x')
        gate = admission.GitHubAdmission(state, 'localhost', POLICY)
        client = transport.GitHubGraphQL(gate, 'localhost', env=self.env, endpoint=f'https://localhost:{self.port}/api/graphql')
        with self.assertRaises(admission.StateError): client.request(QUERY)
        self.assertEqual(self.server.calls, [])


if __name__ == '__main__': unittest.main()
