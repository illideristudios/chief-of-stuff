"""Operator intake is bound to one attempt and exposes durable delivery state."""
import http.client
import json
import re
import sys
import tempfile
import threading
from unittest.mock import patch
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pages
import store

KEY = 'github:example/repo#9'

class WorkerMessageUITest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.pages = self.root / 'pages'; self.pages.mkdir()
        self.tree = self.root / 'trees' / 'one'; self.tree.mkdir(parents=True)
        (self.root / 'CLAUDE.md').write_text('## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n- Worktrees: `trees/`\n- Timezone: America/Chicago\n')
        store.migrate(self.root / 'store.db'); self.db = store.Store(self.root / 'store.db')
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title='Task')
        self.db.record_segment(KEY, 'implement', 1, 'start', runtime='claude')
        self.seg = self.db.segments(KEY)[0]; self.sid = str(uuid.uuid4())
        run = self.root / '.chief-of-stuff' / 'runs' / '9-implement-1'; run.mkdir(parents=True)
        (run / 'command.json').write_text(json.dumps({'session_id': self.sid, 'cwd': str(self.tree), 'env': {'CLAUDE_CONFIG_DIR': str(self.root / 'config')}, 'stdin_prompt': 'start'}))
        self.db.set_resume(KEY, {'runs': {'implement-1': {'session_id': self.sid, 'tree': str(self.tree), 'run_dir': str(run), 'runner_pid': 123, 'runner_identity': 'verified'}}})
        live = patch('worker._runner_live', return_value=True); live.start(); self.addCleanup(live.stop)
        self.server = pages.make_server(self.pages, 0, root=self.root, refresh=lambda *_: None, every=3600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.path = f'/workers/sessions/{self.seg.id}'
    def request(self, method, path, body=None, headers=None):
        port = self.server.server_address[1]; conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        hdr = {'Host': f'127.0.0.1:{port}'}; hdr.update(headers or {})
        conn.request(method, path, body, hdr); resp = conn.getresponse(); result = (resp.status, resp.read().decode()); conn.close(); return result
    def form(self):
        status, html = self.request('GET', self.path); self.assertEqual(status, 200)
        return {key: re.search(r'name="'+key+r'" value="([^"]+)"', html).group(1) for key in ('csrf', 'request_id')}
    def post(self, form, **headers):
        port = self.server.server_address[1]
        return self.request('POST', self.path + '/messages', urlencode(form), {'Origin': f'http://127.0.0.1:{port}', 'Content-Type': 'application/x-www-form-urlencoded', **headers})
    def test_submit_idempotency_status_and_attempt_race(self):
        form = self.form(); form['message'] = '<please check>'
        self.assertEqual(self.post(form)[0], 303)
        self.assertEqual(self.post(form)[0], 303)
        self.assertEqual(len(self.db.worker_messages(KEY)), 1)
        self.assertEqual(self.post({**form, 'message': 'different'})[0], 409)
        html = self.request('GET', self.path)[1]
        self.assertIn('&lt;please check&gt;', html); self.assertIn('Queued', html)
        self.db.end_segment(KEY, 'implement', 1, 'end', 'done')
        self.assertEqual(self.post({**form, 'request_id': str(uuid.uuid4())})[0], 409)
        self.assertEqual(self.post(form)[0], 303)
        self.assertIn('finished', self.request('GET', self.path)[1].lower())
    def test_cross_origin_tokens_and_limits(self):
        form = self.form(); form['message'] = 'hello'
        self.assertEqual(self.post(form, Origin='http://evil.test')[0], 403)
        self.assertEqual(self.post(form, Host='evil.test')[0], 403)
        self.assertEqual(self.post({**form, 'csrf': 'wrong'})[0], 403)
        self.assertEqual(self.post({**form, 'message': 'x' * 20000})[0], 400)
        self.assertEqual(self.post({**form, 'message': 'x' * 30000})[0], 413)
        self.assertEqual(self.db.worker_messages(KEY), [])
    def test_statuses_are_visible_and_never_promise_model_action(self):
        form = self.form(); form['message'] = 'check this'
        self.assertEqual(self.post(form)[0], 303)
        message = self.db.worker_messages(KEY)[0]
        claim = self.db.begin_worker_message_delivery(message.id, KEY, 'implement', 1, self.sid)
        html = self.request('GET', self.path)[1]
        self.assertIn('Sending / unconfirmed', html)
        self.assertIn('will not be resent automatically', html)
        self.db.ack_worker_message(message.id, KEY, 'implement', 1, self.sid,
                                   delivery_id=claim.delivery_id, evidence='CLI replayed the UUID')
        html = self.request('GET', self.path)[1]
        self.assertIn('Delivered to Claude CLI', html); self.assertIn('CLI replayed the UUID', html)
        self.assertIn('not that the worker acted on it', html)
        form = self.form(); form['message'] = 'other'
        self.assertEqual(self.post(form)[0], 303)
        other = self.db.worker_messages(KEY)[1]
        self.db.fail_worker_message(other.id, KEY, 'implement', 1, self.sid, reason='not written')
        self.assertIn('Failed', self.request('GET', self.path)[1])

    def test_concurrent_same_request_creates_only_one_message(self):
        form = self.form(); form['message'] = 'one delivery'
        barrier = threading.Barrier(2); statuses = []
        def send():
            barrier.wait(); statuses.append(self.post(form)[0])
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(sorted(statuses), [303, 303])
        self.assertEqual(len(self.db.worker_messages(KEY)), 1)

    def test_unicode_byte_limit_and_preserved_draft(self):
        form = self.form(); form['message'] = 'é' * 4096
        self.assertEqual(self.post(form)[0], 303)
        form = self.form(); form['message'] = 'é' * 4097
        status, body = self.post(form)
        self.assertEqual(status, 400); self.assertIn('é' * 4097, body)
        self.assertEqual(len(self.db.worker_messages(KEY)), 1)

    def test_authorization_is_bound_to_host_and_session(self):
        form = self.form(); form['message'] = 'hello'
        port = self.server.server_address[1]
        self.assertEqual(self.request('POST', self.path + '/messages', urlencode(form),
                         {'Content-Type': 'application/x-www-form-urlencoded'})[0], 403)
        self.assertEqual(self.post(form, Host=f'localhost:{port}', Origin=f'http://localhost:{port}')[0], 403)
        run = self.db.resume(KEY); run['runs']['implement-1']['session_id'] = str(uuid.uuid4())
        self.db.set_resume(KEY, run)
        self.assertEqual(self.post(form)[0], 403)
        self.assertEqual(self.db.worker_messages(KEY), [])

    def test_message_loop_handoffs_independently_of_forge_pass(self):
        import engine
        event = threading.Event(); called = threading.Event()
        with patch.object(engine.worker, 'deliver_messages', side_effect=lambda *_: called.set()):
            thread = threading.Thread(target=engine._message_loop, args=(self.root, event), kwargs={'every': 0.02})
            thread.start()
            try:
                self.assertTrue(called.wait(2))
            finally:
                event.set(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_unsupported_runtime_does_not_offer_messaging(self):
        self.db.record_segment(KEY, 'review', 1, 'start', runtime='codex')
        segment = next(s for s in self.db.segments(KEY) if s.stage == 'review')
        status, html = self.request('GET', f'/workers/sessions/{segment.id}')
        self.assertEqual(status, 200)
        self.assertIn('not available for this runtime', html)
        self.assertNotIn('name="message"', html)

    def test_dead_runner_and_intake_closed_are_explained(self):
        with patch('worker._runner_live', return_value=False):
            html = self.request('GET', self.path)[1]
        self.assertIn('finished accepting messages', html)
        self.assertNotIn('name="message"', html)

    def test_unsupported_attempt_has_no_composer(self):
        self.db.end_segment(KEY, 'implement', 1, 'end', 'done')
        html = self.request('GET', self.path)[1]
        self.assertIn('finished', html.lower()); self.assertNotIn('name="message"', html)

if __name__ == '__main__': unittest.main()
