"""Board scans use real admitted HTTP attempts across independent workspaces."""
import http.server
import json
import multiprocessing
import os
import shutil
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from test_board_sources import NOW, workspace
from test_github_durable_scan import Forge
import board_sources as bs
import github_admission as admission
import github_graphql_transport as transport
import render_board


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        query = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['query']
        with self.server.guard:
            self.server.active += 1
            self.server.peak = max(self.server.peak, self.server.active)
            self.server.calls.append(query)
        try:
            time.sleep(.015)
            mode = self.server.mode
            if mode == 'cooldown':
                body = {'data': {'rateLimit': {'cost': 1}}}
                status, headers = 403, {'Retry-After': '120'}
            elif mode == 'unknown':
                body, status, headers = {'message': 'unknown'}, 429, {'Retry-After': '120'}
            else:
                _, raw, _ = self.server.forge(['query=' + query])
                body, status, headers = json.loads(raw), 200, {}
            raw = json.dumps(body).encode()
            self.send_response(status)
            for key, value in headers.items(): self.send_header(key, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        finally:
            with self.server.guard: self.server.active -= 1
    def log_message(self, *args): pass


def scan_process(root, state, port, cert, queue):
    policy = admission.Policy(60, 100, 500, 20)
    env = {'GH_ENTERPRISE_TOKEN': 'integration-secret', 'SSL_CERT_FILE': cert,
           'no_proxy': '*', 'PATH': os.environ['PATH']}
    def client(home):
        gate = admission.GitHubAdmission(state, 'localhost', policy, clock=lambda: 1000, lock_timeout=10)
        return transport.GitHubGraphQL(gate, 'localhost', env=env,
                                      endpoint=f'https://localhost:{port}/api/graphql')
    with mock.patch.object(bs, '_github_transport', side_effect=client), mock.patch.object(bs, 'GH_MAX_REQUESTS', 2):
        result = bs.refresh(Path(root), NOW)
        queue.put(result.errors)


class ScanTransportIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(cls.cert), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()

    def setUp(self):
        self.tmpcase = tempfile.TemporaryDirectory(); self.addCleanup(self.tmpcase.cleanup)
        self.root = Path(self.tmpcase.name)
        self.roots = [workspace('GitHub issues; repo o/app') for _ in range(2)]
        for root in self.roots:
            self.addCleanup(shutil.rmtree, root, True)
            with sqlite3.connect(root / 'store.db') as db:
                db.execute('CREATE TABLE work_item (issue_key TEXT)')
                db.execute('CREATE TABLE resume (issue_key TEXT, data TEXT)')
            db.close()
        self.server.guard = threading.Lock()
        self.server.active = self.server.peak = 0
        self.server.calls = []
        self.server.mode = ''
        self.server.forge = Forge(6)
        self.clock = 1000
        self.policy = admission.Policy(60, 100, 500, 20)
        self.env = {'GH_ENTERPRISE_TOKEN': 'integration-secret', 'SSL_CERT_FILE': str(self.cert),
                    'no_proxy': '*', 'PATH': os.environ['PATH']}
        self.patch = mock.patch.object(bs, '_github_transport', side_effect=self.client)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def client(self, home):
        gate = admission.GitHubAdmission(self.root / 'state', 'localhost', self.policy,
                                         clock=lambda: self.clock, lock_timeout=10)
        return transport.GitHubGraphQL(gate, 'localhost', env=self.env,
                endpoint=f'https://localhost:{self.server.server_port}/api/graphql')

    def refresh(self, root, now=NOW):
        return bs.refresh(root, now)

    def test_two_workspaces_share_transport_budget_and_resume_after_restart(self):
        with mock.patch.object(bs, 'GH_MAX_REQUESTS', 2):
            ctx = multiprocessing.get_context('spawn')
            queue = ctx.Queue()
            processes = [ctx.Process(target=scan_process, args=(str(root), str(self.root / 'state'),
                         self.server.server_port, str(self.cert), queue)) for root in self.roots]
            for process in processes: process.start()
            errors = [queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all('merge requests' in error for error in errors))
            self.assertTrue(all((r / 'pages' / '.github-scan.json').exists() for r in self.roots))
            for tick in range(1, 10):
                results = [self.refresh(root, NOW + timedelta(minutes=tick)) for root in self.roots]
                if all('merge requests' not in x.errors for x in results): break
            self.assertTrue(all(len(x.changes) == 6 for x in results))
        self.assertEqual(self.server.peak, 1)
        self.assertGreater(len(self.server.calls), 4)
        ledger = self.client(None).gate.path.read_text()
        self.assertNotIn('integration-secret', ledger)
        self.assertEqual(len(json.loads(ledger)['events']), len(self.server.calls))

    def test_cooldown_and_unknown_outcome_keep_cached_panel_and_report_stale(self):
        for root in self.roots:
            self.assertEqual(len(self.refresh(root).changes), 6)
        self.server.mode = 'unknown'
        before = len(self.server.calls)
        failed = self.refresh(self.roots[0], NOW + timedelta(minutes=1))
        self.assertEqual(len(failed.changes), 6)
        self.assertIn('stale', failed.errors['merge requests'])
        blocked = self.refresh(self.roots[1], NOW + timedelta(minutes=1))
        self.assertEqual(len(self.server.calls), before + 1)
        self.assertEqual(len(blocked.changes), 6)
        self.assertIn('stale', blocked.errors['merge requests'])
        cfg = bs.read_config(self.roots[1])
        tracker = (self.roots[1] / cfg.tracker_path(NOW.date().isoformat())).read_text()
        page = render_board.render(tracker, cfg, NOW, sources=blocked)
        self.assertIn('merge requests: stale', page)
        self.assertNotIn('integration-secret', page)
        self.clock += 121
        self.server.mode = ''
        recovered = self.refresh(self.roots[1], NOW + timedelta(minutes=3))
        self.assertIn('shared budget is stale', recovered.errors['merge requests'])
        self.assertEqual(len(recovered.changes), 6)
        calls = len(self.server.calls)
        again = self.refresh(self.roots[1], NOW + timedelta(minutes=4))
        self.assertGreater(len(self.server.calls), calls)
        self.assertIn('shared budget is stale', again.errors['merge requests'])

    def test_known_cost_http_error_cooldown_blocks_other_workspace(self):
        self.server.mode = 'cooldown'
        failed = self.refresh(self.roots[0])
        self.assertIn('HTTP 403', failed.errors['merge requests'])
        blocked = self.refresh(self.roots[1])
        self.assertIn('cooldown', blocked.errors['merge requests'])
        _, audit_error = bs.change_states(bs.backlog.GitHubBacklog('o/app'), {1001}, bs.backlog.run_gh, None)
        self.assertIn('cooldown', audit_error)
        self.assertEqual(len(self.server.calls), 1)
        self.assertFalse(self.client(None).status()['stale'])


if __name__ == '__main__': unittest.main()
