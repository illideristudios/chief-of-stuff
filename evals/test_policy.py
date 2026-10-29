"""Unit tests for scripts/policy.py and `settings.load_policy`: policy and workflow as data (design decisions 12 and 13).

the owner, 2026-10-10: "everything is policy driven. We have default policies, and based on personas. This is all
configurable at a stage / room level." Plan M4 exit: a gate turned on for one stage or room holds only there, and an
exception with no expiry is refused.
"""

import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import policy  # noqa: E402
import settings  # noqa: E402

BASE = """
default_workflow = "standard"

[policy]
trio = ["reviewer", "designer", "implementer"]

[personas.implementer]
role = "implements"
model = "opus"
effort = "high"
rules = ["write the failing test first"]

[personas.reviewer]
role = "reviews"
model = "opus"

[personas.designer]
role = "designs"
model = "sonnet"

[personas.architect]
role = "holds the architecture"
model = "opus"
agent = "frank-lloyd-aight"
uses = ["reviewer"]

[personas.architect.policy]
blocking_severity = "important"

[stages.implement]
persona = "implementer"

[stages.review]
persona = "reviewer"

[stages.review.policy]
architect = true
zach_gates = ["merge"]

[stages.merge]

[rooms.ops]
persona = "implementer"

[rooms.ops.policy]
retry_limit = 5
zach_gates = ["plan"]

[rooms.lab]

[workflows.standard]
start = "implement"
edges = [
  { from = "implement", to = "review", gate = "plan" },
  { from = "review", to = "implement" },
  { from = "review", to = "merge", gate = "merge" },
]
"""


def parse(text: str = BASE) -> policy.Policies:
    return policy.parse(tomllib.loads(text))


def refused(test: unittest.TestCase, text: str, words: str) -> None:
    with test.assertRaises(policy.PolicyError) as caught:
        parse(text)
    test.assertIn(words, str(caught.exception))


class ShippedPolicyTest(unittest.TestCase):
    def test_the_shipped_file_loads_with_exactly_one_workflow(self):
        got = settings.load_policy()
        self.assertEqual(len(got.workflows), 1)
        self.assertIn(got.default_workflow, got.workflows)

    def test_unsettled_shipped_personas_are_marked_placeholders(self):
        self.assertIn("PLACEHOLDER", settings.POLICY.read_text())
        for name in ("reviewer", "architect"):
            self.assertIn("PLACEHOLDER", settings.load_policy().personas[name].role, name)

    def test_the_shipped_base_needs_no_word_from_zach_and_has_no_limits(self):
        base = settings.load_policy().resolve()
        self.assertEqual(base.zach_gates, ())
        self.assertEqual(base.retry_limit, 3)
        self.assertEqual(base.launch, "adapter")
        self.assertEqual((base.spend_usd, base.quota_percent, base.time_minutes), (None, None, None))


class LoadTest(unittest.TestCase):
    def test_a_file_that_is_not_toml_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "policy.toml")
            path.write_text("[policy\n")
            with self.assertRaises(policy.PolicyError):
                settings.load_policy(path)

    def test_a_named_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "policy.toml")
            path.write_text(BASE)
            self.assertEqual(sorted(settings.load_policy(path).workflows), ["standard"])


class DefaultsTest(unittest.TestCase):
    def test_the_code_defaults(self):
        got = policy.Policy()
        self.assertEqual(got.zach_gates, ())  # decision 6: no gate needs the owner's word by default
        self.assertEqual(got.blocking_severity, "minor")  # burn down to no open findings
        self.assertEqual(got.retry_limit, 3)
        self.assertEqual(got.launch, "adapter")
        self.assertIsNone(got.spend_usd)
        self.assertIsNone(got.quota_percent)
        self.assertIsNone(got.time_minutes)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.p = parse()

    def test_a_gate_turned_on_for_one_stage_holds_only_there(self):
        self.assertEqual(self.p.resolve("reviewer", "review", "lab").zach_gates, ("merge",))
        self.assertEqual(self.p.resolve("reviewer", "implement", "lab").zach_gates, ())
        self.assertEqual(self.p.resolve("implementer", "merge").zach_gates, ())

    def test_a_gate_turned_on_for_one_room_holds_only_there(self):
        self.assertEqual(self.p.resolve("implementer", "implement", "ops").zach_gates, ("plan",))
        self.assertEqual(self.p.resolve("implementer", "implement", "lab").zach_gates, ())
        self.assertEqual(self.p.resolve("implementer", "implement").zach_gates, ())

    def test_the_retry_limit_is_adjustable_per_room(self):
        self.assertEqual(self.p.resolve("implementer", "implement", "ops").retry_limit, 5)
        self.assertEqual(self.p.resolve("implementer", "implement", "lab").retry_limit, 3)

    def test_room_overrides_stage_overrides_persona_overrides_base(self):
        self.assertEqual(self.p.resolve("architect").blocking_severity, "important")
        self.assertEqual(self.p.resolve("reviewer").blocking_severity, "minor")
        self.assertEqual(self.p.resolve("architect", "review", "ops").zach_gates, ("plan",))
        self.assertTrue(self.p.resolve("implementer", "review").architect)
        self.assertFalse(self.p.resolve("implementer", "implement").architect)

    def test_the_base_policy_reaches_every_scope(self):
        self.assertEqual(self.p.resolve("implementer", "implement", "ops").trio, ("reviewer", "designer", "implementer"))

    def test_an_unknown_name_is_refused(self):
        for args in (("nobody",), (None, "nowhere"), (None, None, "attic")):
            with self.subTest(args=args), self.assertRaises(policy.PolicyError):
                self.p.resolve(*args)


class ApprovalTest(unittest.TestCase):
    def test_a_standing_approval_with_an_expiry_loads_and_holds_only_in_its_scope(self):
        p = parse(BASE + '\n[[rooms.lab.policy.approvals]]\ngate = "merge"\nexpires = 2026-10-12T00:00:00-07:00\n')
        approvals = p.resolve("reviewer", "review", "lab").approvals
        self.assertEqual([a.gate for a in approvals], ["merge"])
        self.assertEqual(approvals[0].expires, datetime(2026, 10, 12, tzinfo=timezone(timedelta(hours=-7))))
        self.assertEqual(p.resolve("reviewer", "review", "ops").approvals, ())

    def test_a_standing_approval_with_no_expiry_is_refused(self):
        refused(self, BASE + '\n[[rooms.lab.policy.approvals]]\ngate = "merge"\n', "expires")

    def test_an_expiry_with_no_zone_or_no_time_is_refused(self):
        for when in ("2026-10-12T00:00:00", "2026-10-12"):
            with self.subTest(when=when):
                refused(self, BASE + f'\n[[policy.approvals]]\ngate = "merge"\nexpires = {when}\n', "expires")

    def test_legacy_approval_names_may_outlive_workflow_gates(self):
        self.assertEqual(parse(BASE + '\n[[policy.approvals]]\ngate = "deploy"\nexpires = 2026-10-12T00:00:00Z\n').base.approvals[0].gate, "deploy")


class FieldTest(unittest.TestCase):
    def test_bad_policy_values_are_refused(self):
        for line, words in (
            ('retry_limit = -1', "retry_limit"),
            ('retry_limit = true', "retry_limit"),
            ('blocking_severity = "nit"', "blocking_severity"),
            ('launch = "tmux"', "launch"),
            ('architect = "yes"', "architect"),
            ('zach_gates = [1]', "list of names"),
            ('spend_usd = -1', "spend_usd"),
            ('time_minutes = "1h"', "time_minutes"),
            ('colour = "red"', "colour"),
        ):
            with self.subTest(line=line):
                refused(self, BASE.replace("[policy]\n", f"[policy]\n{line}\n"), words)

    def test_a_limit_must_be_finite(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                refused(self, BASE.replace("[policy]\n", f"[policy]\nspend_usd = {value}\n"), "spend_usd")

    def test_limits_are_fields_with_no_default_value(self):
        got = parse(BASE.replace("[policy]\n", "[policy]\nspend_usd = 20\nquota_percent = 80.5\ntime_minutes = 90\n")).resolve()
        self.assertEqual((got.spend_usd, got.quota_percent, got.time_minutes), (20, 80.5, 90))

    def test_architect_is_refused_in_default_and_tagged_trios_at_every_scope(self):
        for scope in ("policy", "personas.implementer.policy", "stages.implement.policy", "rooms.lab.policy"):
            for field in ('trio = ["reviewer", "architect", "implementer"]',
                          'trios = { design = ["reviewer", "architect", "implementer"] }'):
                with self.subTest(scope=scope, field=field):
                    text = BASE.replace('trio = ["reviewer", "designer", "implementer"]', '')
                    if scope == "policy":
                        text = text.replace("[policy]", "[policy]\n" + field)
                    else:
                        text += f"\n[{scope}]\n{field}\n"
                    refused(self, text, "architect")

    def test_shipped_trio_uses_only_the_three_sonnet_personas(self):
        shipped = settings.load_policy()
        self.assertEqual(shipped.base.trio, ("implementer", "senior-architect", "senior-staff-engineer"))
        self.assertEqual([shipped.personas[n].model for n in shipped.base.trio], ["sonnet"] * 3)
        self.assertTrue(all(shipped.personas[n].agent is None for n in shipped.base.trio))

    def test_a_trio_is_three_distinct_known_personas(self):
        for trio in ('["reviewer", "designer"]', '["reviewer", "reviewer", "designer"]', '["reviewer", "designer", "ghost"]'):
            with self.subTest(trio=trio):
                refused(self, BASE.replace('trio = ["reviewer", "designer", "implementer"]', f"trio = {trio}"), "trio")

    def test_the_architect_is_never_a_trio_member(self):
        # Section 6: the architect is a standing session the engine never starts; the trio would launch it.
        refused(self, BASE.replace('"designer", "implementer"]', '"architect", "implementer"]', 1), "architect")
        refused(self, BASE.replace("[policy]\n", '[policy]\ntrios = { security = ["architect", "reviewer", "designer"] }\n'),
                "architect")
        refused(self, BASE.replace("[rooms.lab]\n", '[rooms.lab]\n[rooms.lab.policy]\ntrio = ["architect", "reviewer", "designer"]\n'),
                "architect")

    def test_the_shipped_policy_has_no_architect_in_its_trio(self):
        got = settings.load_policy().resolve()
        self.assertEqual(len(got.trio), 3)
        self.assertNotIn("architect", got.trio)

    def test_the_trio_map_takes_a_topic_tag_to_three_personas(self):
        got = parse(BASE.replace("[policy]\n", '[policy]\ntrios = { security = ["designer", "reviewer", "implementer"] }\n'))
        self.assertEqual(got.resolve().trios, {"security": ("designer", "reviewer", "implementer")})
        refused(self, BASE.replace("[policy]\n", '[policy]\ntrios = { security = ["architect"] }\n'), "security")

    def test_a_stage_or_persona_can_ask_for_a_full_session(self):
        got = parse(BASE.replace("[stages.merge]\n", '[stages.merge]\n[stages.merge.policy]\nlaunch = "session"\n'))
        self.assertEqual(got.resolve("implementer", "merge").launch, "session")
        self.assertEqual(got.resolve("implementer", "review").launch, "adapter")

    def test_unknown_keys_are_refused(self):
        refused(self, BASE + "\n[teams.x]\n", "teams")
        refused(self, BASE.replace('role = "reviews"', 'role = "reviews"\ncolour = "red"'), "colour")
        refused(self, BASE.replace("[stages.merge]\n", '[stages.merge]\ncolour = "red"\n'), "colour")


class PersonaTest(unittest.TestCase):
    def test_a_persona_and_an_agent(self):
        p = parse()
        self.assertEqual(p.personas["implementer"].rules, ("write the failing test first",))
        self.assertIsNone(p.personas["reviewer"].agent)
        self.assertEqual(p.personas["architect"].agent, "frank-lloyd-aight")
        self.assertEqual(p.personas["architect"].uses, ("reviewer",))

    def test_a_persona_needs_a_role_and_a_model(self):
        refused(self, BASE.replace('role = "reviews"\n', ""), "role")
        refused(self, BASE.replace('role = "reviews"\nmodel = "opus"\n', 'role = "reviews"\n'), "model")

    def test_an_unknown_effort_is_refused(self):
        refused(self, BASE.replace('effort = "high"', 'effort = "huge"'), "effort")

    def test_only_an_agent_uses_personas_and_only_known_ones(self):
        refused(self, BASE.replace('role = "reviews"', 'role = "reviews"\nuses = ["implementer"]'), "uses")
        refused(self, BASE.replace('uses = ["reviewer"]', 'uses = ["ghost"]'), "ghost")

    def test_a_stage_or_room_names_a_known_persona(self):
        refused(self, BASE.replace('[stages.implement]\npersona = "implementer"', '[stages.implement]\npersona = "ghost"'), "ghost")
        refused(self, BASE.replace('[rooms.ops]\npersona = "implementer"', '[rooms.ops]\npersona = "ghost"'), "ghost")


WORKFLOW = BASE.split("[workflows.standard]")[0]


def flow(body: str, extra: str = "") -> str:
    return WORKFLOW + extra + "[workflows.standard]\n" + body


class WorkflowTest(unittest.TestCase):
    def test_the_graph_is_read_from_data(self):
        w = parse().workflows["standard"]
        self.assertEqual(w.start, "implement")
        self.assertEqual([(e.source, e.target, e.gate) for e in w.edges],
                         [("implement", "review", "plan"), ("review", "implement", None), ("review", "merge", "merge")])
        self.assertEqual(w.next("review"), (w.edges[1], w.edges[2]))

    def test_a_workflow_with_no_start_is_refused(self):
        refused(self, flow('edges = [{ from = "implement", to = "merge" }]\n'), "start")

    def test_an_unknown_stage_is_refused(self):
        refused(self, flow('start = "design"\n'), "design")
        refused(self, flow('start = "implement"\nedges = [{ from = "implement", to = "deploy" }]\n'), "deploy")
        refused(self, flow('start = "implement"\nedges = [{ from = "qa", to = "merge" }]\n'), "qa")

    def test_edges_that_are_not_a_list_are_refused(self):
        for edges in ("{}", '"implement"'):
            with self.subTest(edges=edges):
                refused(self, flow(f'start = "implement"\nedges = {edges}\n'), "edges")

    def test_a_start_that_is_not_a_name_is_refused(self):
        refused(self, flow('start = []\n'), "start")

    def test_an_edge_to_nowhere_is_refused(self):
        refused(self, flow('start = "implement"\nedges = [{ from = "implement" }]\n'), "to")

    def test_an_unreachable_stage_is_refused(self):
        refused(self, flow('start = "implement"\nedges = [{ from = "implement", to = "merge" }, { from = "review", to = "merge" }]\n'),
                "review")

    def test_a_graph_with_no_end_is_refused(self):
        refused(self, flow('start = "implement"\nedges = [{ from = "implement", to = "review" }, { from = "review", to = "implement" }]\n'),
                "end")

    def test_a_single_stage_workflow_is_whole(self):
        self.assertEqual(parse(BASE + '\n[workflows.solo]\nstart = "merge"\n').workflows["solo"].edges, ())

    def test_the_issue_type_selects_the_workflow(self):
        p = parse(BASE + '\n[workflows.hotfix]\nstart = "implement"\nissue_types = ["bug"]\n'
                         'edges = [{ from = "implement", to = "merge", gate = "merge" }]\n')
        self.assertEqual(p.workflow_for("bug").name, "hotfix")
        self.assertEqual(p.workflow_for("feature").name, "standard")
        self.assertEqual(p.workflow_for(None).name, "standard")

    def test_an_issue_type_two_workflows_claim_is_refused(self):
        refused(self, BASE.replace('start = "implement"\nedges', 'start = "implement"\nissue_types = ["bug"]\nedges', 1)
                + '\n[workflows.hotfix]\nstart = "merge"\nissue_types = ["bug"]\n', "bug")

    def test_the_default_workflow_must_exist(self):
        refused(self, BASE.replace('default_workflow = "standard"', 'default_workflow = "fast"'), "default_workflow")
        refused(self, BASE.replace('default_workflow = "standard"\n', ""), "default_workflow")
        for bad in ("[]", "{}"):
            refused(self, BASE.replace('default_workflow = "standard"', f"default_workflow = {bad}"), "default_workflow")



class OverlayTest(unittest.TestCase):
    """#827: a workspace's overlay merges keys over the shipped policy; it adds no table."""

    def shipped(self):
        return tomllib.loads(BASE)

    def test_a_stage_policy_key_is_merged_over_the_shipped_table(self):
        merged = policy.overlay(self.shipped(), {"stages": {"review": {"policy": {"architect": False}}}})
        got = policy.parse(merged)
        self.assertFalse(got.resolve("reviewer", "review").architect)
        self.assertEqual(got.resolve("reviewer", "review").zach_gates, ("merge",))  # a sibling key is kept
        self.assertTrue(parse().resolve("reviewer", "review").architect)  # the shipped data is not changed

    def test_a_field_is_replaced_whole(self):
        merged = policy.overlay(self.shipped(), {"policy": {"trio": ["implementer", "reviewer", "designer"]}})
        self.assertEqual(policy.parse(merged).resolve().trio, ("implementer", "reviewer", "designer"))

    def test_a_section_the_shipped_policy_lacks_is_refused_with_its_name(self):
        for over, name in (({"rooms": {"attic": {}}}, "rooms.attic"), ({"stages": {"deploy": {}}}, "stages.deploy"),
                           ({"colours": {}}, "colours")):
            with self.subTest(name=name), self.assertRaises(policy.PolicyError) as caught:
                policy.overlay(self.shipped(), over)
            self.assertIn(name, str(caught.exception))

    def test_an_unknown_key_is_refused_with_its_name(self):
        for over, name in (({"policy": {"colour": "red"}}, "colour"),
                           ({"stages": {"review": {"colour": "red"}}}, "colour"),
                           ({"stages": {"review": {"policy": {"colour": "red"}}}}, "colour")):
            with self.subTest(over=over), self.assertRaises(policy.PolicyError) as caught:
                policy.parse(policy.overlay(self.shipped(), over))
            self.assertIn(name, str(caught.exception))

if __name__ == "__main__":
    unittest.main()
