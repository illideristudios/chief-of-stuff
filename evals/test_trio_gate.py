"""The decision trio adopts only three matching option choices (design 5a, plan M5 items 1-2)."""

import ast
import json
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import decision_actions  # noqa: E402
import decision_escalation  # noqa: E402
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
        self.assertEqual((row.answer, row.decided_by), (None, "owner"))
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
                self.assertEqual((row.answer, row.decided_by),
                                 (None, "" if kind in model_port.RETRYABLE else "owner"))
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
                self.assertEqual((row.answer, row.decided_by), (None, "owner"))
                self.assertEqual(row.record["verdicts"][1]["vote"], "concern")
                self.assertIn("malformed", row.record["verdicts"][1]["reasoning"])

    def test_three_adopt_votes_split_across_options_escalate(self):
        got = self.consult([reply(), reply(choice="Keep the parser"), reply()])
        self.assertTrue(got.escalated)
        self.assertFalse(got.unanimous)
        self.assertIsNone(got.recommendation)
        row, = self.store.decisions(KEY)
        self.assertEqual((row.answer, row.decided_by), (None, "owner"))
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
                self.assertEqual((row.answer, row.decided_by), (None, "owner"))
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
            "verdicts": [{"persona": name, "vote": "adopt", "choice": "Split the parser", "reasoning": reason,
                          "runtime": "claude", "model": PERSONAS[name].model}  # #879: each vote names what cast it
                         for name, reason in zip(DEFAULT, reasons)],
            "recommendation": "Split the parser", "unanimous": True,
            "default": "The worker waits: the issue holds until this is answered.",  # #1128
        })

    def test_direct_answer_settlement_belongs_to_decision_actions(self):
        ask_id = self.store.add_ask(KEY, "implement", 1, "session", self.decision.question, list(OPTIONS), "ctx")
        decision = replace(self.decision, topic="ask", instance=f"ask-{ask_id}", context="ctx")
        self.consult([reply("concern")] * 3, decision=decision)
        self.store.answer_ask(KEY, ask_id, OPTIONS[1])
        ask, = self.store.asks(KEY)
        settled = decision_escalation.answer_ask(self.store.decisions(KEY), ask)
        self.assertEqual((settled.topic, settled.question, settled.answer, settled.decided_by),
                         ("ask", ask.question, OPTIONS[1], "user"))
        self.assertEqual(settled.record["recommendation"], OPTIONS[1])
        self.assertFalse(settled.record["unanimous"])
        self.assertEqual(settled.record["decision"]["instance"], f"ask-{ask_id}")
        from types import SimpleNamespace
        for field, value in (("context", "other"), ("options", ["New", "Old"])):
            mismatched = SimpleNamespace(**{name: getattr(ask, name) for name in
                                           ("id", "question", "options", "context", "answer")})
            setattr(mismatched, field, value)
            self.assertIsNone(decision_escalation.answer_ask(self.store.decisions(KEY), mismatched))
        other = SimpleNamespace(**{name: getattr(ask, name) for name in
                                  ("id", "question", "options", "context", "answer")})
        other.id = ask_id + 1
        created = decision_escalation.answer_ask(self.store.decisions(KEY), other)
        self.assertEqual(created.record["decision"]["instance"], f"ask-{ask_id + 1}")
        self.store.add_decision(KEY, settled.topic, settled.question, settled.answer, settled.decided_by, settled.record)
        self.assertIsNone(decision_escalation.answer_ask(self.store.decisions(KEY), ask))

    def test_needs_owner_reads_only_the_latest_row_for_a_decision_attempt(self):
        row = decision_escalation.DecisionUpdate("plan", "Gate?", None, "owner", {
            "error": "gate unavailable", "decision": {
                "stage": "implement", "attempt": 1, "context": "implement-1"}})
        expected = decision_escalation.NeedsOwner(("plan", "Gate?", ""), "implement", 1, "gate unavailable")
        self.assertEqual(decision_escalation.needs_owner([row]), expected)
        # Split, hold and exhaustion need no special eligibility rule.
        for record in ({}, {"recommendation": "hold"}, {"retry_exhausted": 8}, None):
            pending = replace(row, record=record)
            self.assertIsNotNone(decision_escalation.needs_owner([pending]))
        for settled in (replace(row, answer="approve"), replace(row, decided_by="trio"),
                        replace(row, decided_by="")):
            self.assertIsNone(decision_escalation.needs_owner([row, settled]))
        self.assertIsNone(decision_escalation.needs_owner([]))

    def test_a_new_context_cannot_reuse_an_unanswered_exhaustion(self):
        decision = replace(self.decision, context="implement-1")
        self.store.add_decision(KEY, decision.topic, decision.question, None, "owner", {
            "retry_exhausted": 8, "decision": asdict(decision)})
        model = FakeModel([reply(choice=OPTIONS[1])] * 3)
        got = trio_gate.decide(replace(decision, context="implement-2"), self.store.decisions(KEY),
                               self.policy, PERSONAS, model, self.store)
        self.assertEqual((got.recommendation, len(model.requests)), (OPTIONS[1], 3))
        self.assertEqual([r.record["decision"]["context"] for r in self.store.decisions(KEY)],
                         ["implement-1", "implement-2"])

    def test_the_same_decision_id_in_a_new_stage_attempt_is_consulted_again(self):
        first = replace(self.decision, stage="implement", attempt=1)
        self.consult([reply()] * 3, decision=first)
        model = FakeModel([reply(choice=OPTIONS[1])] * 3)
        second = replace(first, attempt=2)
        got = trio_gate.decide(second, self.store.decisions(KEY), self.policy, PERSONAS, model, self.store)
        self.assertEqual((got.recommendation, len(model.requests)), (OPTIONS[1], 3))
        self.assertEqual([r.record["decision"]["attempt"] for r in self.store.decisions(KEY)], [1, 2])

    def test_a_gate_hold_is_recorded_as_unanswered_for_the_owner(self):
        decision = replace(self.decision, options=("approve", "hold"), stage="implement", attempt=1, gate=True)
        self.consult([reply(choice="hold")] * 3, decision=decision)
        row, = self.store.decisions(KEY)
        self.assertEqual((row.answer, row.decided_by), (None, "owner"))
        self.assertEqual(row.record["recommendation"], "hold")

    def test_invalid_options_are_escalated_and_deduplicated_by_the_use_case(self):
        decision = replace(self.decision, options=(), topic="ask", instance="ask-1",
                           context="ctx", stage="implement", attempt=1)
        self.store.add_decision(KEY, decision.topic, decision.question, record={
            "decision": asdict(decision), "answer_ask_id": 17})
        model = FakeModel([])
        for _ in range(2):
            got = trio_gate.decide(decision, self.store.decisions(KEY), self.policy, PERSONAS, model, self.store)
            self.assertTrue(got.escalated)
        self.assertEqual(model.requests, [])
        self.assertEqual(len(self.store.decisions(KEY)), 2)
        row = self.store.decisions(KEY)[-1]
        self.assertIsNotNone(decision_escalation.needs_owner([row]))
        self.assertEqual(row.record["answer_ask_id"], 17)
        self.assertFalse(row.record["unanimous"])
        self.assertEqual(row.record["decision"], {**asdict(decision), "options": [], "tags": ["design"]})
        self.assertIn("at least two", row.record["error"])

    def test_replacement_ask_specification_and_link_are_decision_updates(self):
        from types import SimpleNamespace
        origin = SimpleNamespace(topic="design", question=self.decision.question, record={
            "decision": {"options": list(OPTIONS), "context": "original", "instance": "parser"}})
        segment = SimpleNamespace(stage="implement", attempt=2)
        context = decision_escalation.replacement_context(origin, segment)
        record = {"decision": {**origin.record["decision"], "context": context}, "unanimous": False}
        row = SimpleNamespace(topic=origin.topic, question=origin.question, answer=None,
                              decided_by="owner", record=record)
        spec, existing, pending = decision_escalation.replacement_ask(origin, segment, [row], [])
        self.assertIsInstance(pending, decision_escalation.DecisionUpdate)
        self.assertIsNone(existing)
        self.assertEqual(spec, dict(stage="implement", attempt=2, session_id="", question=row.question,
                                    options=list(OPTIONS), context=context))
        update = decision_escalation.link_ask(pending, 42)
        self.assertIsInstance(update, decision_escalation.DecisionUpdate)
        self.assertEqual((update.topic, update.question, update.answer, update.decided_by),
                         (row.topic, row.question, None, "owner"))
        self.assertEqual(update.record, {**record, "answer_ask_id": 42})
        self.assertNotIn("answer_ask_id", row.record)

    def test_decision_actions_reverse_and_restarted_manage_board_reversals(self):
        self.consult([reply()] * 3)
        rev = decision_escalation.reverse(self.store.decisions(KEY), self.decision.topic, self.decision.question)
        self.store.add_decision(KEY, topic=rev.topic, question=rev.question,
                                answer=rev.answer, decided_by=rev.decided_by, record=rev.record)
        first, second = self.store.decisions(KEY)
        self.assertEqual(first.answer, "Split the parser")
        self.assertIsNone(second.answer)
        self.assertTrue(second.record["reversed"])
        self.assertEqual(second.record["recommendation"], "Split the parser")
        self.assertEqual(second.record["decision"]["options"], list(OPTIONS))
        with self.assertRaisesRegex(ValueError, "adopted"):
            decision_escalation.reverse(self.store.decisions(KEY), self.decision.topic, self.decision.question)
        restarted = decision_escalation.restarted(self.store.decisions(KEY), "implement")
        self.assertEqual(len(restarted), 1)
        self.store.add_decision(KEY, topic=restarted[0].topic, question=restarted[0].question,
                                answer=restarted[0].answer, decided_by=restarted[0].decided_by,
                                record=restarted[0].record)
        self.assertEqual(decision_escalation.restarted(self.store.decisions(KEY), "implement"), [])

    def test_decide_reuses_matching_decision_and_reconsults_on_new_options_context_reversal_or_retryable_failure(self):
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
        rev = decision_escalation.reverse(self.store.decisions(KEY), refined.topic, refined.question)
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



E = lambda runtime, model, effort=None: SimpleNamespace(runtime=runtime, model=model, effort=effort)  # noqa: E731
ROTATION = (E("claude", "claude-sonnet-5-5", "high"), E("cursor", "auto"), E("cursor", "composer-2.5"),
            E("codex", "gpt-6.1-sol", "high"), E("agy", "gemini-4-argon-high"))


class TrioModelsTest(unittest.TestCase):
    """#879 (§6): the trio draws distinct models from [models.trio]; a persona's model is only the fallback."""
    setUp = GateTest.setUp

    def port(self, answer=None, rotation=ROTATION, pick="first", launchable=lambda runtime: True):
        self.requests = []

        def call(request):
            self.requests.append(request)
            return answer(request) if answer else reply()
        return model_port.Port(call, rotation, pick, launchable)

    def ask(self, port):
        return trio_gate.consult(self.decision, self.policy, PERSONAS, port, self.store)

    def test_three_distinct_models_on_three_runtimes_are_drawn_and_each_vote_names_its_model(self):
        self.assertTrue(self.ask(self.port()).unanimous)
        drawn = [(r.runtime, r.model, r.effort) for r in self.requests]
        self.assertEqual(drawn, [("claude", "claude-sonnet-5-5", "high"), ("cursor", "auto", ""), ("codex", "gpt-6.1-sol", "high")])
        row, = self.store.decisions(KEY)
        self.assertEqual([(v["runtime"], v["model"]) for v in row.record["verdicts"]], [d[:2] for d in drawn])
        self.assertTrue(row.record["diverse"])

    def test_random_pick_draws_in_a_random_order(self):
        with mock.patch.object(model_port.random, "sample", side_effect=lambda xs, n: list(reversed(xs))):
            self.ask(self.port(pick="random"))
        self.assertEqual([(r.runtime, r.model) for r in self.requests],
                         [("agy", "gemini-4-argon-high"), ("codex", "gpt-6.1-sol"), ("cursor", "composer-2.5")])

    def test_no_rotation_uses_each_personas_own_model(self):
        self.ask(self.port(rotation=()))
        self.assertEqual([(r.runtime, r.model) for r in self.requests], [("", PERSONAS[n].model) for n in DEFAULT])
        self.assertNotIn("diverse", self.store.decisions(KEY)[0].record)

    def test_fewer_than_three_launchable_entries_are_reused_and_recorded_as_not_diverse(self):
        self.ask(self.port(launchable=lambda runtime: runtime == "cursor"))
        self.assertEqual([r.model for r in self.requests], ["auto", "composer-2.5", "auto"])
        self.assertFalse(self.store.decisions(KEY)[0].record["diverse"])

    def test_a_quota_stop_moves_that_member_to_the_next_distinct_entry(self):
        quota = lambda request: model_port.Failure("quota", "429 quota") if request.runtime == "cursor" else reply()  # noqa: E731
        self.assertTrue(self.ask(self.port(quota)).unanimous)
        votes = self.store.decisions(KEY)[0].record["verdicts"]
        # cursor:auto stops, then cursor:composer-2.5; the member lands on an entry no other member stopped on.
        self.assertEqual([(v["runtime"], v["model"]) for v in votes],
                         [("claude", "claude-sonnet-5-5"), ("agy", "gemini-4-argon-high"), ("codex", "gpt-6.1-sol")])

    def test_a_timed_out_or_unparsed_vote_is_replaced_by_another_entry_once(self):
        # #928: a Cursor timeout or an agy answer with no structure is an outage, not a concern.
        for kind in ("unknown", "error"):
            with self.subTest(kind=kind):
                failing = lambda request: model_port.Failure(kind, "cursor timed out") if request.runtime == "cursor" else reply()  # noqa: E731
                self.assertTrue(self.ask(self.port(failing)).unanimous)
                votes = self.store.decisions(KEY)[-1].record["verdicts"]
                self.assertEqual([v["runtime"] for v in votes], ["claude", "agy", "codex"])
                self.assertEqual([r.runtime for r in self.requests], ["claude", "cursor", "agy", "codex"])

    def test_a_replacement_that_also_fails_keeps_the_failed_vote(self):
        got = self.ask(self.port(lambda request: model_port.Failure("unknown", "timed out")
                                 if request.runtime in ("cursor", "agy") else reply()))
        self.assertTrue(got.escalated)
        self.assertEqual([r.runtime for r in self.requests], ["claude", "cursor", "agy", "codex"])
        self.assertEqual(self.store.decisions(KEY)[-1].decided_by, "")  # retryable, not yet the owner's

    def test_a_call_gets_at_most_the_time_left_and_none_starts_once_it_is_gone(self):
        # #927 review: the pass budget bounds every call, not only the start of a consult.
        left = [trio_gate.TIMEOUT_S + 30.0]

        def first_uses_it_all(request):
            left[0] = 0.0
            return reply()
        got = trio_gate.consult(self.decision, self.policy, PERSONAS, self.port(first_uses_it_all), self.store,
                                remaining=lambda: left[0])
        self.assertTrue(got.escalated)
        self.assertEqual([r.timeout_s for r in self.requests], [trio_gate.TIMEOUT_S])  # min(TIMEOUT_S, more than that); no further call
        row = self.store.decisions(KEY)[-1]
        self.assertEqual(row.decided_by, "")
        self.assertTrue(decision_escalation.retryable_failure(row.record))
        left[0] = 30.0
        trio_gate.consult(replace(self.decision, question="Short?"), self.policy, PERSONAS, self.port(), self.store,
                          remaining=lambda: left[0])
        self.assertEqual({r.timeout_s for r in self.requests}, {30.0})

    def test_with_no_entry_left_after_quota_stops_the_owner_decides(self):
        got = self.ask(self.port(lambda request: model_port.Failure("quota", "429 quota")))
        self.assertTrue(got.escalated)
        row = self.store.decisions(KEY)[-1]
        self.assertEqual((row.answer, row.decided_by), (None, "owner"))
        self.assertTrue(row.record["retry_exhausted"])
        self.assertIn("every [models.trio] entry stopped on quota", row.record["error"])

    def test_a_rotation_with_nothing_launchable_goes_to_the_owner(self):
        got = self.ask(self.port(launchable=lambda runtime: False))
        self.assertTrue(got.escalated)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.decisions(KEY)[-1].decided_by, "owner")


class DefaultTest(unittest.TestCase):
    """#1128: every recorded decision carries its default, what the engine does if nobody answers it (§6)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.db"
        store.migrate(path)
        self.store = store.Store(path)
        self.addCleanup(self.store.engine.dispose)
        self.store.upsert_item(KEY, title="Separate parsing")
        self.policy = policy.Policy(trio=DEFAULT)

    def split(self, decision):
        trio_gate.consult(decision, self.policy, PERSONAS, FakeModel([reply(vote="concern")] * 3), self.store)
        return self.store.decisions(KEY)[-1]

    def test_each_kind_of_decision_records_the_engines_no_answer_outcome(self):
        permission = json.dumps({"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "make"}})
        kinds = {
            # engine._route_asks denies a permission the trio does not adopt
            "Denied": trio_gate.Decision(KEY, "ask", "Permission requested for Bash", ("allow", "deny"),
                                         context=permission, instance="ask-1"),
            # reconcile._gate waits at a gate until its answer is approve
            "Hold": trio_gate.Decision(KEY, "plan", "gate requires the owner's approval", ("approve", "hold"),
                                       context="implement-1", gate=True),
            # engine_mcp's ask: the worker reports blocked and waits for the engine
            "The worker waits": trio_gate.Decision(KEY, "ask", "Which parser?", OPTIONS, instance="ask-2"),
        }
        for words, decision in kinds.items():
            with self.subTest(topic=decision.topic, question=decision.question):
                self.assertTrue(self.split(decision).record["default"].startswith(words))

    def test_an_adopted_decision_and_an_unavailable_trio_carry_it_too(self):
        decision = trio_gate.Decision(KEY, "ask", "Which parser?", OPTIONS, instance="ask-3")
        trio_gate.consult(decision, self.policy, PERSONAS, FakeModel([reply()] * 3), self.store)
        self.assertTrue(self.store.decisions(KEY)[-1].record["default"].startswith("The worker waits"))
        gate = trio_gate.Decision(KEY, "plan", "gate requires the owner's approval", ("approve",), gate=True)
        for _ in range(2):  # refused every pass, recorded once
            trio_gate.decide(gate, self.store.decisions(KEY), self.policy, PERSONAS, None, self.store)
        rows = [r for r in self.store.decisions(KEY) if r.topic == "plan"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].record["default"].startswith("Hold"))

    def test_a_board_answer_or_reversal_keeps_the_decisions_default(self):
        decision = trio_gate.Decision(KEY, "scope", "Approve this file ownership?", ("approve", "reject"))
        asked = self.split(decision)
        tr = decision_actions.answer(asked, "approve", decided_by="user")
        trio_gate.record(self.store, KEY, tr.update)
        answered = self.store.decisions(KEY)[-1]
        self.assertEqual(answered.record["default"], asked.record["default"])
        trio_gate.record(self.store, KEY, decision_escalation.reverse(self.store.decisions(KEY), "scope", decision.question))
        self.assertEqual(self.store.decisions(KEY)[-1].record["default"], asked.record["default"])


class DrawTest(unittest.TestCase):
    """The pure selection beside model_port's rotation (#879, DRY with #853)."""

    def test_different_runtimes_come_first_then_other_entries(self):
        self.assertEqual(model_port.draw(ROTATION, 4), (ROTATION[0], ROTATION[1], ROTATION[3], ROTATION[4]))
        self.assertEqual(model_port.draw(ROTATION[1:3], 3), ROTATION[1:3])

    def test_used_entries_are_skipped_and_their_runtimes_count_as_drawn(self):
        self.assertEqual(model_port.draw(ROTATION, 1, used=(ROTATION[0], ROTATION[1])), (ROTATION[3],))

    def test_only_launchable_entries_are_drawn(self):
        self.assertEqual(model_port.draw(ROTATION, 3, launchable=lambda runtime: runtime == "codex"), (ROTATION[3],))


class PublicImportTest(unittest.TestCase):
    """trio_gate reaches decision_escalation through public names only (#1131)."""

    def test_trio_gate_imports_no_private_name(self):
        self.assertTrue(callable(getattr(decision_escalation, "is_gate", None)))
        tree = ast.parse(Path(trio_gate.__file__).read_text(encoding="utf-8"))
        private = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                   for alias in node.names if alias.name.startswith("_")]
        self.assertEqual(private, [])


if __name__ == "__main__":
    unittest.main()
