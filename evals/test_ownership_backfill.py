"""Backfill proposes bounded claims; only a reviewed, unchanged plan can write."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import migrate_backlog as mb
import ownership


SOURCES = {'scripts/widget.py': 'def repair():\n    pass\n',
           'evals/test_widget.py': 'import widget\n',
           'docs/design.md': '# Design\n'}


def validator(bodies):
    # A stand-in for #820's issue_gate parser, whose missing-section result is None.
    return [None if '## File ownership' not in b else ownership.issue_scopes(b) for b in bodies]


def issue(number=1, title='Fix widget.py', body='Repair `widget.py`.'):
    return dict(number=number, title=title, body=body, state='OPEN')


class DerivationTest(unittest.TestCase):
    def test_resolves_unique_basename_and_companion_test_from_code(self):
        scopes, why = ownership.derive_scopes('Fix widget.py', 'Repair `widget.py`.', SOURCES)
        self.assertEqual(dict(scopes), {'scripts/widget.py': 'write', 'evals/test_widget.py': 'write'})
        self.assertFalse(why)

    def test_read_only_feature_under_review_does_not_make_fix_task_read_only(self):
        scopes, why = ownership.derive_scopes('Fix widget.py', 'Found in review of the read-only widget task.', SOURCES)
        self.assertEqual(dict(scopes), {'scripts/widget.py': 'write', 'evals/test_widget.py': 'write'})
        self.assertFalse(why)

    def test_comparative_example_alone_is_not_an_implementation_target(self):
        scopes, why = ownership.derive_scopes('Implement merge ordering', 'Use both hosts, like `widget`.', SOURCES)
        self.assertEqual(scopes, [])
        self.assertTrue(why)

    def test_read_only_requires_explicit_task_intent(self):
        scopes, why = ownership.derive_scopes('Read-only audit', 'Inspect `widget.py`; read-only.', SOURCES)
        self.assertEqual(scopes, [('scripts/widget.py', 'read')])
        self.assertFalse(why)

    def test_ambiguous_basename_and_unnamed_scope_require_the_owner(self):
        sources = dict(SOURCES, **{'other/widget.py': ''})
        for body in ('Repair `widget.py`.', 'Fix the engine.', 'Needs the whole repository.'):
            with self.subTest(body=body):
                scopes, why = ownership.derive_scopes('Fix', body, sources)
                self.assertEqual(scopes, [])
                self.assertTrue(why)

    def test_reference_documents_do_not_become_the_implementation_scope(self):
        scopes, why = ownership.derive_scopes('Implement stage M4', 'Plan: `docs/design.md`.', SOURCES)
        self.assertEqual(scopes, [])
        self.assertTrue(why)

    def test_stage_words_are_not_inferred_as_python_functions(self):
        scopes, why = ownership.derive_scopes('Implement fix stage', 'The `fix` stage needs a policy.', {'scripts/tasks.py': 'def fix():\n    pass\n'})
        self.assertEqual(scopes, [])
        self.assertTrue(why)

    def test_explicit_function_call_resolves_to_its_definition(self):
        scopes, why = ownership.derive_scopes('Fix', 'Repair `repair()`.', SOURCES)
        self.assertIn(('scripts/widget.py', 'write'), scopes)
        self.assertFalse(why)

    def test_explicit_do_not_edit_paths_cannot_be_granted_write_access(self):
        scopes, why = ownership.derive_scopes('Fix widget.py', 'Do not edit `docs/design.md`.', SOURCES)
        self.assertEqual(dict(scopes), {'docs/design.md': 'read', 'scripts/widget.py': 'write', 'evals/test_widget.py': 'write'})
        self.assertFalse(why)

    def test_companion_tests_respect_explicit_edit_prohibitions(self):
        scopes, why = ownership.derive_scopes('Fix widget.py', 'Do not edit `evals/test_widget.py`.', SOURCES)
        self.assertEqual(dict(scopes), {'scripts/widget.py': 'write', 'evals/test_widget.py': 'read'})
        self.assertFalse(why)

    def test_directory_prohibition_cannot_be_bypassed_by_named_child(self):
        scopes, why = ownership.derive_scopes('Fix widget.py', 'Do not edit `scripts/`.', SOURCES)
        self.assertEqual(scopes, [])
        self.assertTrue(why)

    def test_read_only_checkbox_is_a_task_intent(self):
        scopes, why = ownership.derive_scopes('Audit', '- [ ] Read-only: inspect `widget.py`.', SOURCES)
        self.assertEqual(scopes, [('scripts/widget.py', 'read')])
        self.assertFalse(why)

    def test_read_instructions_keep_reference_documents_read_only(self):
        scopes, why = ownership.derive_scopes('Repair widget.py', 'Read `docs/design.md` first.', SOURCES)
        self.assertIn(('docs/design.md', 'read'), scopes)
        self.assertIn(('scripts/widget.py', 'write'), scopes)
        self.assertFalse(why)

    def test_explicit_path_wins_over_ambiguous_basename(self):
        scopes, why = ownership.derive_scopes('Fix', '`scripts/widget.py:23`', dict(SOURCES, **{'other/widget.py': ''}))
        self.assertIn(('scripts/widget.py', 'write'), scopes)
        self.assertFalse(why)

    def test_new_paths_without_code_are_reported_and_examples_are_not_claimed(self):
        scopes, why = ownership.derive_scopes('Fix', 'Create `scripts/new.py`. Example:\n```\nother/fake.py\n```', SOURCES)
        self.assertEqual(scopes, [])
        self.assertTrue(why)


class PlanTest(unittest.TestCase):
    def test_existing_declarations_are_kept_and_unknowns_are_reported(self):
        existing = issue(2, body='## File ownership\n- read: `.`\n')
        plan = mb.ownership_plan('example/repo', [issue(), existing, issue(3, 'Unclear', 'Please fix it')], SOURCES, validator)
        self.assertEqual([r['status'] for r in plan['issues']], ['derived', 'existing', 'unresolved'])
        self.assertEqual(plan['issues'][1]['section'], '')
        self.assertIn('## File ownership', plan['issues'][0]['section'])
        self.assertIn('the owner', mb.ownership_report([plan]))

    def test_legacy_fallback_parser_is_refused(self):
        with self.assertRaisesRegex(ValueError, '#820'):
            mb.ownership_plan('example/repo', [issue()], SOURCES, lambda bodies: [ownership.issue_scopes(b) for b in bodies])

    def test_parser_rejection_never_becomes_a_proposal(self):
        plan = mb.ownership_plan('example/repo', [issue()], SOURCES, lambda bodies: [None] * len(bodies))
        self.assertEqual(plan['issues'][0]['status'], 'unresolved')

    def test_replacement_keeps_every_other_section(self):
        body = 'Intro\n\n## File ownership\n- typo: old\n\n## Acceptance\nKeep this.\n'
        got = ownership.with_issue_scopes(body, [('scripts/widget.py', 'write')])
        self.assertEqual(got, 'Intro\n\n## File ownership\n- write: `scripts/widget.py`\n\n## Acceptance\nKeep this.\n')

    def test_duplicate_and_fenced_headings_require_the_owner(self):
        for body in ('## File ownership\n- bad\n## File ownership\n- bad', '```md\n## File ownership\n- write: `.`\n```'):
            plan = mb.ownership_plan('example/repo', [issue(body=body)], SOURCES, validator)
            self.assertEqual(plan['issues'][0]['status'], 'unresolved')


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.row = issue()
        self.plan = {'version': 1, 'workspaces': [mb.ownership_plan('example/repo', [self.row], SOURCES, validator)]}
        self.writes = []
        self.head = 'a' * 40
        self.comments = []
        self.plan_text = json.dumps(self.plan, indent=2) + '\n'
        self.approved_text = self.plan_text

    def gh(self, args, body=None):
        if args[:2] == ['pr', 'view']:
            return {'headRefOid': self.head, 'comments': self.comments, 'url': 'https://github.com/example/repo/pull/7'}
        if args[0] == 'api':
            import base64
            return {'content': base64.b64encode(self.approved_text.encode()).decode()}
        if args[:2] == ['issue', 'view']:
            return self.row
        if args[:2] == ['issue', 'edit']:
            self.writes.append((args, body))
            return {}
        self.fail(str(args))

    def apply(self):
        return mb.apply_ownership(self.plan, self.plan_text, 'https://github.com/example/repo/pull/7',
                                  'docs/reports/ownership-plan.json', self.gh, validator)

    def approve(self):
        self.comments = [{'body': f'Architect: APPROVED {self.head}'}]

    def test_unapproved_or_stale_approval_writes_nothing(self):
        for comments in ([], [{'body': 'Architect: APPROVED ' + 'b' * 40}], [{'body': 'Quoted Architect: APPROVED ' + self.head}]):
            self.comments = comments
            with self.assertRaisesRegex(ValueError, 'approval'):
                self.apply()
        self.assertEqual(self.writes, [])

    def test_later_changes_revokes_approval(self):
        self.approve()
        self.comments.append({'body': f'Architect: CHANGES {self.head}'})
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.apply()
        self.assertEqual(self.writes, [])

    def test_last_verdict_in_a_comment_controls_approval(self):
        self.comments = [{'body': f'Architect: APPROVED {self.head}\nArchitect: CHANGES {self.head}'}]
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.apply()
        self.assertEqual(self.writes, [])

    def test_one_issue_failure_does_not_lose_the_rest_of_the_report(self):
        plan = mb.ownership_plan('example/repo', [issue(), issue(2)], SOURCES, validator)
        self.plan['workspaces'] = [plan]
        self.plan_text = json.dumps(self.plan, indent=2) + '\n'
        self.approved_text = self.plan_text
        self.approve()
        real_gh = self.gh
        def gh(args, body=None):
            if args[:3] == ['issue', 'edit', '1']:
                raise ValueError('temporary failure')
            return real_gh(args, body)
        got = mb.apply_ownership(self.plan, self.plan_text, 'https://github.com/example/repo/pull/7', 'docs/reports/ownership-plan.json', gh, validator)
        self.assertEqual([r['status'] for r in got], ['failed', 'written'])

    def test_approved_plan_writes_through_gh_and_is_idempotent(self):
        self.approve()
        results = self.apply()
        self.assertEqual(results[0]['status'], 'written')
        self.assertEqual(len(self.writes), 1)
        self.assertIn('--body-file', self.writes[0][0])
        self.assertIn('## File ownership', self.writes[0][1])
        self.row['body'] = self.writes[0][1]
        self.assertEqual(self.apply()[0]['status'], 'already applied')
        self.assertEqual(len(self.writes), 1)

    def test_changed_issue_closed_issue_and_unreviewed_plan_are_not_written(self):
        self.approve()
        for change in ({'body': 'Changed'}, {'state': 'CLOSED'}):
            self.row = dict(issue(), **change)
            self.assertEqual(self.apply()[0]['status'], 'skipped')
        self.approved_text = '{}'
        with self.assertRaisesRegex(ValueError, 'reviewed plan'):
            self.apply()
        self.assertEqual(self.writes, [])

    def test_dry_run_cli_never_writes_issues_and_saves_report_and_plan(self):
        with tempfile.TemporaryDirectory() as d, patch.object(mb, 'ownership_sources', return_value=SOURCES), patch.object(mb, 'ownership_validator', return_value=validator), patch.object(mb, 'ownership_gh', return_value=[issue()]) as gh:
            report = Path(d) / 'report.md'
            code = mb.main(['--file-ownership', '--workspace', 'example/repo=.', '--dry-run', '--report', str(report)])
            self.assertEqual(code, 0)
            self.assertTrue(report.exists())
            self.assertTrue(report.with_suffix('.json').exists())
            self.assertFalse(any(c.args[0][:2] == ['issue', 'edit'] for c in gh.call_args_list))


if __name__ == '__main__':
    unittest.main()
