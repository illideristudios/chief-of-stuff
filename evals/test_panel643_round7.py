"""Round-seven redesign: the page server records an intent, `decision_actions` picks the current one, the engine applies it."""
import http.client
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode

from evals import test_engine as engines
import decision_actions as da
import decision_escalation
import pages

KEY = engines.KEY


class IntentEndToEndTest(unittest.TestCase):
    setUp = engines.EngineTest.setUp
    run_pass = engines.EngineTest.run_pass
    launch = engines.EngineTest.launch

    def post(self, data):
        (self.root / "pages").mkdir(exist_ok=True)
        server = pages.make_server(self.root / "pages", 0, root=self.root, refresh=lambda *a: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        with mock.patch.object(pages.forge, "for_workspace", side_effect=AssertionError("the page server never writes")):
            conn.request("POST", "/", urlencode(data))
            response = conn.getresponse()
            response.read()
        conn.close()
        return response.status

    def asked(self, topic, question, answer=""):
        self.db.sync_from_forge(self.forge.snapshots())
        return self.db.add_decision(KEY, topic, question, answer or None, "user" if answer else "owner")

    def current(self, row):
        """A decision's state: its identity's latest row (rows are append-only)."""
        return da.state_of(decision_escalation.latest(self.db.decisions(KEY))[decision_escalation.identity(row)])

    def test_a_reroute_is_written_to_the_forge_by_the_engine_only(self):
        dec = self.asked("design", "Back?")
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "review"}), 303)
        self.assertEqual(self.forge.writes, [])
        self.run_pass()
        self.assertEqual(self.forge.writes, [(1, "review", "implement", True)])
        self.assertEqual(self.launched, [(KEY, "review", 1, "critic")])

    def test_a_reversal_after_a_reroute_leaves_only_the_reversal_to_run(self):
        first = self.asked("design", "Back?")
        second = self.asked("scope", "Ship?", "Yes")
        self.assertEqual(self.post({"decision_id": first.id, "action": "reroute", "target_stage": "review"}), 303)
        self.assertEqual(self.post({"decision_id": second.id, "action": "reverse", "answer": "No"}), 303)
        began = self.db.decisions(KEY)[-1]  # the reversal intent begins on this row
        self.run_pass()
        reroute, reversal = self.current(first), self.current(second)
        self.assertEqual((reroute.status, reroute.superseded_by, reroute.intent), ("superseded", began.id, ""))
        self.assertEqual((reversal.status, reversal.intent), ("reversed", ""))
        self.assertEqual((self.forge.writes, self.launched), ([], [(KEY, "implement", 1, "builder")]))

    def test_a_reversal_runs_past_a_reroute_the_retry_limit_blocked(self):
        first = self.asked("design", "Back?")
        self.assertEqual(self.post({"decision_id": first.id, "action": "reroute", "target_stage": "review"}), 303)
        reroute = self.db.decisions(KEY)[-1]
        self.db.add_decision(KEY, reroute.topic, reroute.question, "Back", "user",
                             {"status": "blocked", "blocked_reason": "retry limit reached", "rerouted_to": "review",
                              "intent": "run", "intent_id": reroute.id})
        self.run_pass()
        self.assertEqual(self.launched, [])
        second = self.asked("scope", "Ship?", "Yes")
        self.assertEqual(self.post({"decision_id": second.id, "action": "reverse", "answer": "No"}), 303)
        self.run_pass()
        self.assertEqual(self.current(reroute).status, "superseded")
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])


if __name__ == "__main__":
    unittest.main()
