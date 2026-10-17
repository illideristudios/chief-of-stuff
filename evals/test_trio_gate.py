"""The decision trio adopts only three matching option choices (design 5a, plan M5 items 1-2)."""

import json
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import model_port  # noqa: E402
import policy  # noqa: E402
import store  # noqa: E402
import trio_gate  # noqa: E402

KEY = "github:example/app#7"
DEFAULT = ("reviewer", "designer", "implementer")
TAGGED = ("operator", "reviewer", "designer")
OPTIONS = ("Split the parser", "Keep the parser")
PERSONAS = {
    "reviewer": policy.Persona("reviewer", "checks correctness", "review-model", "high", ("Explain risks",)),
    "designer": policy.Persona("designer", "checks boundaries", "design-model", "max"),
    "implementer": policy.Persona("implementer", "checks feasibility", "build-model"),
    "operator": policy.Persona("operator", "checks operations", "ops-model", "medium"),
}


def reply(vote="adopt", choice="Split the parser", reasoning="Keeps parsing independent"):
    return model_port.Reply("", {"vote": vote, "choice": choice, "reasoning": reasoning}, "", 0, 1)


class FakeModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return next(self.replies)


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.policy = policy.Policy(trio=DEFAULT, trios={"operations": TAGGED, "design": DEFAULT})

    def test_a_matching_tag_selects_its_trio_without_a_model(self):
        got = trio_gate.select(self.policy, PERSONAS, ("unmapped", "operations"))
        self.assertEqual(got, tuple(PERSONAS[name] for name in TAGGED))

    def test_no_tags_or_no_matching_tag_selects_the_default(self):
        for tags in ((), ("unmapped",)):
            with self.subTest(tags=tags):
                self.assertEqual(trio_gate.select(self.policy, PERSONAS, tags),
                                 tuple(PERSONAS[name] for name in DEFAULT))

    def test_multiple_matches_use_the_first_in_decision_tag_order(self):
        self.assertEqual(tuple(p.name for p in trio_gate.select(self.policy, PERSONAS, ("design", "operations"))), DEFAULT)
        self.assertEqual(tuple(p.name for p in trio_gate.select(self.policy, PERSONAS, ("operations", "design"))), TAGGED)

    def test_missing_duplicate_or_unknown_personas_are_policy_errors(self):
        for names in ((), ("reviewer", "designer"), (*DEFAULT, "operator"),
                      ("reviewer", "reviewer", "designer"), ("reviewer", "designer", "unknown")):
            for tagged in (False, True):
                with self.subTest(names=names, tagged=tagged):
                    p = replace(self.policy, **({"trios": {"operations": names}} if tagged else {"trio": names}))
                    with self.assertRaisesRegex(policy.PolicyError, "three distinct known personas"):
                        trio_gate.select(p, PERSONAS, ("operations",) if tagged else ())

    def test_the_resolved_room_and_stage_policy_controls_selection(self):
        policies = policy.Policies(
            self.policy, PERSONAS,
            {"review": policy.Place("review", policy={"trios": {"design": TAGGED}})},
            {"lab": policy.Place("lab", policy={"trio": TAGGED})}, {}, "")
        self.assertEqual(tuple(p.name for p in trio_gate.select(policies.resolve(stage="review"), PERSONAS, ("design",))), TAGGED)
        self.assertEqual(tuple(p.name for p in trio_gate.select(policies.resolve(room="lab"), PERSONAS, ())), TAGGED)


class GateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.db"
        store.migrate(path)
        self.store = store.Store(path)
        self.addCleanup(self.store.engine.dispose)
        self.store.upsert_item(KEY, title="Separate parsing")
        self.policy = policy.Policy(trio=DEFAULT, trios={"operations": TAGGED})
        self.decision = trio_gate.Decision(KEY, "architecture", "Should parsing be separate?", OPTIONS, ("design",))

    def consult(self, replies, decision=None, resolved=None):
        self.model = FakeModel(replies)
        return trio_gate.consult(decision or self.decision, resolved or self.policy, PERSONAS, self.model, self.store)

    def test_three_adopts_on_the_same_option_adopt_even_with_different_reasoning(self):
        got = self.consult([reply(reasoning=f"Reason {i}") for i in range(3)])
        self.assertTrue(got.unanimous)
        self.assertFalse(got.escalated)
        self.assertEqual(got.recommendation, "Split the parser")
        row, = self.store.decisions(KEY)
        self.assertEqual((row.answer, row.decided_by), ("Split the parser", "trio"))

    def test_each_persona_gets_a_structured_request_with_its_model_effort_and_profile(self):
        decision = replace(self.decision, options=(*OPTIONS, "Defer the decision"), context="parser context")
        self.consult([reply()] * 3, decision=decision)
        self.assertEqual(len(self.model.requests), 3)
        for request, name in zip(self.model.requests, DEFAULT):
            with self.subTest(persona=name):
                persona = PERSONAS[name]
                self.assertIsInstance(request, model_port.Request)
                self.assertEqual((request.model, request.effort), (persona.model, persona.effort or ""))
                self.assertEqual((request.plugin_dirs, request.session_id, request.resume), ((), "", False))
                self.assertEqual(request.timeout_s, trio_gate.TIMEOUT_S)
                self.assertGreater(request.timeout_s, 0)
                self.assertEqual(request.schema["type"], "object")
                self.assertEqual(set(request.schema["required"]), {"vote", "choice", "reasoning"})
                self.assertEqual(request.schema["properties"]["vote"]["enum"], ["adopt", "concern"])
                self.assertEqual(request.schema["properties"]["choice"]["enum"], list(decision.options))
                self.assertFalse(request.schema["additionalProperties"])
                for field in ("choice", "reasoning"):
                    self.assertEqual(request.schema["properties"][field]["type"], "string")
                self.assertIn(persona.name, request.prompt)
                self.assertIn(persona.role, request.prompt)
                for rule in persona.rules:
                    self.assertIn(rule, request.prompt)
                self.assertIn(self.decision.question, request.prompt)
                self.assertIn(self.decision.topic, request.prompt)
                self.assertIn("design", request.prompt)
                self.assertIn("parser context", request.prompt)
                for option in decision.options:
                    self.assertIn(option, request.prompt)
                self.assertIn("choose", request.prompt.lower())

    def test_the_tagged_trio_is_the_one_consulted(self):
        self.consult([reply()] * 3, decision=replace(self.decision, tags=("operations",)))
        self.assertEqual([r.model for r in self.model.requests], [PERSONAS[n].model for n in TAGGED])
        self.assertEqual([v["persona"] for v in self.store.decisions(KEY)[0].record["verdicts"]], list(TAGGED))

    def test_one_concern_escalates_and_keeps_all_three_verdicts(self):
        got = self.consult([reply(), reply("concern", "Keep the parser", "Too much coupling"), reply()])
        self.assertTrue(got.escalated)
        self.assertFalse(got.unanimous)
        self.assertIsNone(got.recommendation)
        self.assertEqual(len(self.model.requests), 3)
        row, = self.store.decisions(KEY)
        self.assertEqual((row.answer, row.decided_by), (None, "zach"))
        self.assertEqual([v["vote"] for v in row.record["verdicts"]], ["adopt", "concern", "adopt"])
        self.assertEqual(row.record["verdicts"][1]["reasoning"], "Too much coupling")
        self.assertIsNone(row.record["recommendation"])
        self.assertFalse(row.record["unanimous"])

    def test_a_failure_is_a_concern_and_does_not_skip_the_other_personas(self):
        for kind in ("quota", "unknown", "auth", "error"):
            with self.subTest(kind=kind):
                got = self.consult([model_port.Failure(kind, "No judgement available"), reply(), reply()])
                self.assertTrue(got.escalated)
                self.assertEqual(len(self.model.requests), 3)
                row = self.store.decisions(KEY)[-1]
                self.assertEqual((row.answer, row.decided_by), (None, "zach"))
                verdict = row.record["verdicts"][0]
                self.assertEqual(verdict["vote"], "concern")
                self.assertIsNone(verdict["choice"])
                self.assertIn(kind, verdict["reasoning"])
                self.assertIn("No judgement available", verdict["reasoning"])

    def test_malformed_data_is_a_concern_even_if_the_reply_text_says_adopt(self):
        valid = reply().data
        malformed = [None, "adopt", [], {}, {**valid, "vote": "yes"}, {**valid, "vote": []},
                     {**valid, "choice": None}, {**valid, "choice": 7},
                     {**valid, "choice": "   "}, {**valid, "reasoning": False},
                     {**valid, "reasoning": ""}, {**valid, "extra": "unexpected"}]
        malformed += [{k: v for k, v in valid.items() if k != missing} for missing in valid]
        for data in malformed:
            with self.subTest(data=data):
                bad = replace(reply(), data=data, text=json.dumps(valid))
                got = self.consult([reply(), bad, reply()])
                self.assertTrue(got.escalated)
                self.assertIsNone(got.recommendation)
                self.assertEqual(len(self.model.requests), 3)
                row = self.store.decisions(KEY)[-1]
                self.assertEqual((row.answer, row.decided_by), (None, "zach"))
                self.assertEqual(row.record["verdicts"][1]["vote"], "concern")
                self.assertIn("malformed", row.record["verdicts"][1]["reasoning"])

    def test_three_adopt_votes_split_across_options_escalate(self):
        got = self.consult([reply(), reply(choice="Keep the parser"), reply()])
        self.assertTrue(got.escalated)
        self.assertFalse(got.unanimous)
        self.assertIsNone(got.recommendation)
        row, = self.store.decisions(KEY)
        self.assertEqual((row.answer, row.decided_by), (None, "zach"))
        self.assertEqual([v["vote"] for v in row.record["verdicts"]], ["adopt"] * 3)
        self.assertEqual([v["choice"] for v in row.record["verdicts"]], [OPTIONS[0], OPTIONS[1], OPTIONS[0]])

    def test_a_choice_outside_the_options_is_a_malformed_concern(self):
        for choice in ("Rewrite the parser", "Split the parser "):
            with self.subTest(choice=choice):
                got = self.consult([reply(), reply(choice=choice), reply()])
                self.assertTrue(got.escalated)
                self.assertFalse(got.unanimous)
                self.assertIsNone(got.recommendation)
                row = self.store.decisions(KEY)[-1]
                self.assertEqual((row.answer, row.decided_by), (None, "zach"))
                verdict = row.record["verdicts"][1]
                self.assertEqual(verdict["vote"], "concern")
                self.assertIsNone(verdict["choice"])
                self.assertIn("malformed", verdict["reasoning"])
                self.assertEqual(len(self.model.requests), 3)

    def test_invalid_options_raise_before_model_calls_and_write_no_decision(self):
        for options in ((), (OPTIONS[0],), (OPTIONS[0], OPTIONS[0]), (*OPTIONS, OPTIONS[0]),
                        (OPTIONS[0], ""), (OPTIONS[0], "   "), (OPTIONS[0], None), (OPTIONS[0], 7)):
            with self.subTest(options=options):
                model = FakeModel([reply()] * 3)
                with self.assertRaises(ValueError):
                    decision = replace(self.decision, options=options)
                    trio_gate.consult(decision, self.policy, PERSONAS, model, self.store)
                self.assertEqual(model.requests, [])
                self.assertEqual(self.store.decisions(KEY), [])

    def test_the_full_record_round_trips_on_the_issue(self):
        reasons = ["Easy to test", "Preserves boundaries", "Small change"]
        self.consult([reply(reasoning=reason) for reason in reasons])
        row, = self.store.decisions(KEY)
        self.assertEqual((row.issue_key, row.topic, row.question), (KEY, self.decision.topic, self.decision.question))
        self.assertEqual(row.record, {
            "decision": {**asdict(self.decision), "options": list(self.decision.options), "tags": list(self.decision.tags)},
            "verdicts": [{"persona": name, "vote": "adopt", "choice": "Split the parser", "reasoning": reason}
                         for name, reason in zip(DEFAULT, reasons)],
            "recommendation": "Split the parser", "unanimous": True,
        })

    def test_decision_actions_reverse_and_restarted_manage_board_reversals(self):
        import decision_actions
        self.consult([reply()] * 3)
        rev = decision_actions.reverse(self.store.decisions(KEY), self.decision.topic, self.decision.question)
        self.store.add_decision(KEY, topic=rev.topic, question=rev.question,
                                answer=rev.answer, decided_by=rev.decided_by, record=rev.record)
        first, second = self.store.decisions(KEY)
        self.assertEqual(first.answer, "Split the parser")
        self.assertIsNone(second.answer)
        self.assertTrue(second.record["reversed"])
        self.assertEqual(second.record["recommendation"], "Split the parser")
        self.assertEqual(second.record["decision"]["options"], list(OPTIONS))
        with self.assertRaisesRegex(ValueError, "adopted"):
            decision_actions.reverse(self.store.decisions(KEY), self.decision.topic, self.decision.question)
        restarted = decision_actions.restarted(self.store.decisions(KEY), "implement")
        self.assertEqual(len(restarted), 1)
        self.store.add_decision(KEY, topic=restarted[0].topic, question=restarted[0].question,
                                answer=restarted[0].answer, decided_by=restarted[0].decided_by,
                                record=restarted[0].record)
        self.assertEqual(decision_actions.restarted(self.store.decisions(KEY), "implement"), [])

    def test_decide_reuses_matching_decision_and_reconsults_on_new_options_context_reversal_or_retryable_failure(self):
        import decision_actions
        model = FakeModel([reply()] * 12)
        got = trio_gate.decide(self.decision, self.store.decisions(KEY), self.policy, PERSONAS, model, self.store)
        self.assertEqual((got.recommendation, got.unanimous, len(model.requests)), ("Split the parser", True, 3))
        # Exact same decision reuses the settled row without calling the model.
        got = trio_gate.decide(self.decision, self.store.decisions(KEY), self.policy, PERSONAS, model, self.store)
        self.assertEqual((got.recommendation, got.unanimous, len(model.requests)), ("Split the parser", True, 3))
        # Refined options or new context reconsults the trio.
        refined = replace(self.decision, options=("Keep the parser", "Delete the parser"))
        got = trio_gate.decide(refined, self.store.decisions(KEY), self.policy, PERSONAS,
                               FakeModel([reply(choice="Keep the parser")] * 3), self.store)
        self.assertEqual((got.recommendation, got.unanimous), ("Keep the parser", True))
        # A reversed or restarted row is unsettled and reconsults the trio.
        rev = decision_actions.reverse(self.store.decisions(KEY), refined.topic, refined.question)
        self.store.add_decision(KEY, topic=rev.topic, question=rev.question,
                                answer=rev.answer, decided_by=rev.decided_by, record=rev.record)
        got = trio_gate.decide(refined, self.store.decisions(KEY), self.policy, PERSONAS,
                               FakeModel([reply(choice="Delete the parser")] * 3), self.store)
        self.assertEqual((got.recommendation, got.unanimous), ("Delete the parser", True))
        # A retryable model failure (quota/unknown) is retried on the next pass; a non-retryable split stays settled.
        q_dec = replace(self.decision, question="Retryable?")
        trio_gate.decide(q_dec, self.store.decisions(KEY), self.policy, PERSONAS,
                         FakeModel([model_port.Failure("quota", "429"), reply(), reply()]), self.store)
        retry_model = FakeModel([reply()] * 3)
        got = trio_gate.decide(q_dec, self.store.decisions(KEY), self.policy, PERSONAS, retry_model, self.store)
        self.assertEqual((got.recommendation, got.unanimous, len(retry_model.requests)), ("Split the parser", True, 3))

    def test_invalid_selection_makes_no_model_calls_and_writes_no_decision(self):
        for resolved in (policy.Policy(), policy.Policy(trio=("reviewer", "reviewer", "designer")),
                         policy.Policy(trio=DEFAULT, trios={"design": ("reviewer", "designer", "missing")})):
            with self.subTest(resolved=resolved):
                with self.assertRaises(policy.PolicyError):
                    self.consult([reply()] * 3, resolved=resolved)
                self.assertEqual(self.model.requests, [])
                self.assertEqual(self.store.decisions(KEY), [])


if __name__ == "__main__":
    unittest.main()
