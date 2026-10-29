import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import github_admission as admission
import github_graphql_transport as transport

POLICY = admission.Policy(60, 20, 100, 10)
QUERY = 'query { viewer { login } rateLimit { cost remaining limit } }'

# A gh executable: wire bytes come from a fixture, and it never prints its env.
FAKE = '''#!{python}
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ['FAKE_ROOT'])
with (root / 'calls').open('a') as f: f.write(json.dumps([sys.argv[1:], json.load(sys.stdin), os.getcwd()]) + '\\n')
mode = os.environ.get('FAKE_MODE', '')
if mode in ('orphan', 'orphan-exit'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    (root / 'pid').write_text(str(child.pid))
    if mode == 'orphan': time.sleep(30)
if mode == 'slow': time.sleep(.3)
if mode == 'large': sys.stdout.write('x' * 1000000); sys.stdout.flush()
else: sys.stdout.buffer.write((root / 'response').read_bytes()); sys.stdout.flush()
sys.stderr.write(os.environ.get('FAKE_STDERR', ''))
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
'''


def stopped(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # Linux can retain a killed orphan as a zombie until PID 1 reaps it.
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z'
    except FileNotFoundError:
        return sys.platform.startswith('linux')



def worker(state, host, root, work, queue):
    os.chdir(work)
    try:
        gate = admission.GitHubAdmission(state, host, POLICY, lock_timeout=3)
        answer = transport.GitHubGraphQL(gate, host, executable=str(Path(root) / 'gh'),
                                         env={**os.environ, 'FAKE_ROOT': root, 'FAKE_MODE': 'slow'}).request(QUERY)
        queue.put(('ok', answer.cost))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'gh'
        self.bin.write_text(FAKE.format(python=sys.executable))
        self.bin.chmod(0o700)
        self.now = 1000
        self.gate = admission.GitHubAdmission(self.root / 'state', 'github.com', POLICY, clock=lambda: self.now)
        self.env = {**os.environ, 'FAKE_ROOT': str(self.root), 'GH_TOKEN': 'top-secret'}
        self.client = transport.GitHubGraphQL(self.gate, 'github.com', executable=str(self.bin), env=self.env)

    def response(self, status=200, headers='', body=None):
        body = body if body is not None else {'data': {'viewer': {'login': 'user'}, 'rateLimit': {'cost': 2, 'remaining': 4000, 'limit': 5000}}}
        (self.root / 'response').write_text(f'HTTP/2.0 {status} Test\n{headers}\r\n' + json.dumps(body))

    def test_success_and_one_attempt_with_environment_and_stdin(self):
        self.response(headers='X-RateLimit-Remaining: 4000\r\n')
        result = self.client.request(QUERY)
        self.assertEqual((result.cost, result.status, result.body['data']['viewer']['login']), (2, 200, 'user'))
        self.assertFalse(result.admission['stale'])
        args, payload, _ = json.loads((self.root / 'calls').read_text())
        self.assertEqual(args, ['api', 'graphql', '--hostname', 'github.com', '--include', '--method', 'POST', '--input', '-'])
        self.assertEqual(payload, {'query': QUERY})
        self.assertNotIn('top-secret', (self.root / 'calls').read_text() + self.gate.path.read_text())

    def test_http_error_commits_retry_after_and_sanitizes(self):
        self.response(403, 'Retry-After: 120\r\n', {'data': {'rateLimit': {'cost': 1}}, 'message': 'top-secret'})
        self.env['FAKE_EXIT'] = '1'; self.env['FAKE_STDERR'] = 'top-secret'
        with self.assertRaises(transport.TransportError) as e: self.client.request(QUERY)
        self.assertNotIn('top-secret', str(e.exception))
        self.assertEqual(e.exception.status, 403)
        self.assertEqual(self.gate.status()['cooldown_until'], 1120)
        with self.assertRaises(admission.CooldownActive): self.client.request(QUERY)
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 1)
        self.assertNotIn('top-secret', self.gate.path.read_text())

    def test_graphql_200_secondary_errors_commit_cooldown(self):
        self.env['FAKE_EXIT'] = '1'
        self.response(headers='Retry-After: 120\r\n', body={'data': {'rateLimit': {'cost': 1}}, 'errors': [{'type': 'RATE_LIMITED', 'message': 'secondary rate limit'}]})
        result = self.client.request(QUERY)
        self.assertTrue(result.body['errors'])
        self.assertEqual(result.admission['cooldown_until'], 1120)
        with self.assertRaises(admission.CooldownActive): self.client.request(QUERY)

    def test_missing_cost_keeps_cooldown_and_stale(self):
        self.response(429, 'Retry-After: 120\r\n', {'message': 'rate limited'})
        self.env['FAKE_EXIT'] = '1'
        with self.assertRaises(admission.StateError): self.client.request(QUERY)
        state = self.client.status()
        self.assertTrue(state['stale'])
        self.assertEqual((state['recovery_until'], state['cooldown_until']), (1060, 1120))

    def test_retry_is_a_new_admitted_attempt_after_reset(self):
        self.response(403, 'X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1100\r\n',
                      {'data': {'rateLimit': {'cost': 1}}, 'message': 'rate limited'})
        self.env['FAKE_EXIT'] = '1'
        with self.assertRaises(transport.TransportError): self.client.request(QUERY)
        with self.assertRaises(admission.CooldownActive): self.client.request(QUERY)
        self.now = 1100
        self.env['FAKE_EXIT'] = '0'
        self.response()
        self.assertEqual(self.client.request(QUERY).cost, 2)
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 2)
        self.assertEqual([event[1] for event in json.loads(self.gate.path.read_text())['events']], [2])

    def test_non_json_429_keeps_retry_after(self):
        (self.root / 'response').write_bytes(b'HTTP/1.1 429 Too Many Requests\nRetry-After: 120\r\n\r\nservice unavailable')
        self.env['FAKE_EXIT'] = '1'
        with self.assertRaises(admission.StateError): self.client.request(QUERY)
        state = self.client.status()
        self.assertTrue(state['stale'])
        self.assertEqual(state['cooldown_until'], 1120)

    def test_secondary_message_on_non_200_without_retry_after(self):
        self.response(403, body={'data': {'rateLimit': {'cost': 1}}, 'message': 'You have exceeded a secondary rate limit'})
        self.env['FAKE_EXIT'] = '1'
        with self.assertRaises(transport.TransportError): self.client.request(QUERY)
        self.assertEqual(self.client.status()['cooldown_until'], 1060)

    def test_invalid_and_bounded_output_are_unknown(self):
        for wire, mode in [(b'bad', ''), (b'', 'large'), (b'HTTP/2 200 OK\r\nRetry-After: 1\r\nretry-after: 2\r\n\r\n{}', '')]:
            with self.subTest(mode=mode, wire=wire[:10]), tempfile.TemporaryDirectory() as state:
                self.gate = admission.GitHubAdmission(state, 'github.com', POLICY, clock=lambda: self.now)
                self.env['FAKE_MODE'] = mode
                self.client = transport.GitHubGraphQL(self.gate, 'github.com', executable=str(self.bin), env=self.env, max_output=4096)
                (self.root / 'response').write_bytes(wire)
                with self.assertRaises(transport.TransportError): self.client.request(QUERY)
                self.assertTrue(self.client.status()['stale'])

    def test_timeout_kills_descendants_and_quarantines(self):
        self.response(); self.env['FAKE_MODE'] = 'orphan'
        self.client = transport.GitHubGraphQL(self.gate, 'github.com', executable=str(self.bin), env=self.env, timeout=.4)
        with self.assertRaises(transport.TransportError) as e: self.client.request(QUERY)
        self.assertEqual(e.exception.kind, 'timeout')
        pid = int((self.root / 'pid').read_text())
        for _ in range(100):
            if stopped(pid): break
            time.sleep(.01)
        else: self.fail('descendant survived process group cleanup')
        self.assertTrue(self.client.status()['stale'])
        with self.assertRaises(admission.HostQuarantined): self.client.request(QUERY)

    def test_child_holding_pipes_after_cli_exit_is_killed(self):
        self.response(); self.env['FAKE_MODE'] = 'orphan-exit'
        self.client = transport.GitHubGraphQL(self.gate, 'github.com', executable=str(self.bin), env=self.env, timeout=.4)
        with self.assertRaises(transport.TransportError) as exc: self.client.request(QUERY)
        self.assertEqual(exc.exception.kind, 'timeout')
        pid = int((self.root / 'pid').read_text())
        for _ in range(100):
            if stopped(pid): break
            time.sleep(.01)
        else: self.fail('orphan survived process group cleanup')
        self.assertTrue(self.client.status()['stale'])

    def test_enterprise_host_and_ledger_identity(self):
        gate = admission.GitHubAdmission(self.root / 'state', 'enterprise.example', POLICY, clock=lambda: self.now)
        self.response()
        env = {**self.env, 'GH_HOST': 'github.com', 'GH_ENTERPRISE_TOKEN': 'enterprise-secret'}
        client = transport.GitHubGraphQL(gate, 'enterprise.example', executable=str(self.bin), env=env)
        self.assertEqual(client.request(QUERY).cost, 2)
        args, _, _ = json.loads((self.root / 'calls').read_text())
        self.assertEqual(args[3], 'enterprise.example')
        self.assertNotIn('enterprise-secret', gate.path.read_text())
        with self.assertRaises(ValueError): transport.GitHubGraphQL(gate, 'github.com', executable=str(self.bin))
        with self.assertRaises(ValueError): transport.GitHubGraphQL(gate, 'https://enterprise.example', executable=str(self.bin))

    def test_cross_process_workspaces_share_budget(self):
        self.response(); root = str(self.root); state = str(self.root / 'shared')
        workspaces = [self.root / 'a', self.root / 'b']
        for work in workspaces: work.mkdir()
        q = multiprocessing.Queue()
        processes = [multiprocessing.Process(target=worker, args=(state, 'github.com', root, str(work), q)) for work in workspaces]
        for p in processes: p.start()
        for p in processes: p.join(6); self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted(q.get(timeout=1) for _ in processes), [('ok', 2), ('ok', 2)])
        calls = [json.loads(line) for line in (self.root / 'calls').read_text().splitlines()]
        self.assertEqual({c[2] for c in calls}, {str(p.resolve()) for p in workspaces})
        gate = admission.GitHubAdmission(state, 'github.com', POLICY)
        self.assertEqual(sum(e[1] for e in json.loads(gate.path.read_text())['events']), 4)

    def test_cooldown_crosses_processes_and_workspaces(self):
        self.response(429, 'Retry-After: 120\r\n', {'data': {'rateLimit': {'cost': 1}}, 'message': 'limited'})
        state = str(self.root / 'shared'); root = str(self.root)
        one, two = self.root / 'one', self.root / 'two'
        one.mkdir(); two.mkdir()
        q = multiprocessing.Queue()
        for work, expected in ((one, 'TransportError'), (two, 'CooldownActive')):
            p = multiprocessing.Process(target=worker, args=(state, 'github.com', root, str(work), q))
            p.start(); p.join(6)
            self.assertEqual(p.exitcode, 0)
            self.assertEqual(q.get(timeout=1)[0], expected)
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 1)

    def test_unavailable_state_never_dispatches(self):
        state = self.root / 'file'; state.write_text('x')
        gate = admission.GitHubAdmission(state, 'github.com', POLICY)
        client = transport.GitHubGraphQL(gate, 'github.com', executable=str(self.bin), env=self.env)
        with self.assertRaises(admission.StateError): client.request(QUERY)
        self.assertFalse((self.root / 'calls').exists())


if __name__ == '__main__': unittest.main()
