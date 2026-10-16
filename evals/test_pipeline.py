"""Pipeline reads use the workspace's configured forge, including self-hosted GitLab."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backlog
import forge
import pipeline_cli

SHA = "a" * 40
GL = backlog.Backlog("https://labs.example.test/gitlab", "team/nested/app")
GH = backlog.GitHubBacklog("example/app")


def gitlab_row(**changes):
    return {"id": 71, "ref": "main", "sha": SHA, "status": "success",
            "web_url": f"{GL.host}/{GL.project}/-/pipelines/71", **changes}


def github_row(**changes):
    return {"id": 71, "head_branch": "main", "head_sha": SHA,
            "status": "completed", "conclusion": "success",
            "html_url": "https://github.com/example/app/actions/runs/71", **changes}


class PipelineCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "CLAUDE.md"
        self.config.write_text(
            "## Coordinator\n- User: Example\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Backlog: GitLab issues; host {GL.host}; project {GL.project}\n")

    def test_ref_uses_the_self_hosted_backlog_with_the_forge_mocked(self):
        output = io.StringIO()
        homes = []

        def branch_head(adapter, ref):
            homes.append(adapter.home)
            self.assertEqual(ref, "main")
            return forge.Pipeline(71, ref, SHA, "passed", gitlab_row()["web_url"])

        with mock.patch.object(forge.Forge, "branch_head", branch_head), redirect_stdout(output):
            code = pipeline_cli.main(["--root", str(self.root), "--ref", "main"])
        self.assertEqual(code, 0)
        self.assertEqual(homes, [GL])
        for value in ("71", "main", SHA, "passed", gitlab_row()["web_url"]):
            self.assertIn(value, output.getvalue())

    def test_id_uses_the_configured_github_repository(self):
        self.config.write_text(self.config.read_text().replace(
            f"GitLab issues; host {GL.host}; project {GL.project}", "GitHub issues; repo example/app"))
        output = io.StringIO()
        with mock.patch.object(forge.Forge, "pipeline", autospec=True,
                               return_value=forge.Pipeline(71, "main", SHA, "failed", "url")) as read:
            with redirect_stdout(output):
                self.assertEqual(pipeline_cli.main(["--root", str(self.root), "--id", "71"]), 0)
        adapter, number = read.call_args.args
        self.assertEqual(adapter.home, GH)
        self.assertEqual(number, 71)
        self.assertIn("failed", output.getvalue())  # a readable failed pipeline is a successful read

    def test_forge_error_is_nonzero_and_prints_no_result(self):
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(forge.Forge, "pipeline", side_effect=forge.ForgeError("HTTP 404")):
            with redirect_stdout(output), redirect_stderr(error):
                code = pipeline_cli.main(["--root", str(self.root), "--id", "71"])
        self.assertNotEqual(code, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("pipeline: HTTP 404", error.getvalue())

    def test_missing_backlog_is_a_configuration_error(self):
        self.config.write_text(self.config.read_text().split("- Backlog:")[0])
        with redirect_stderr(io.StringIO()):
            self.assertNotEqual(pipeline_cli.main(["--root", str(self.root), "--ref", "main"]), 0)

    def test_requires_exactly_one_selector_and_a_positive_id(self):
        for args in ([], ["--id", "71", "--ref", "main"], ["--id", "zero"], ["--id", "0"],
                     ["--id", "-1"], ["--ref", ""]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    pipeline_cli.main(["--root", str(self.root), *args])
                self.assertNotEqual(raised.exception.code, 0)

    def test_installed_command_routes_to_pipeline_help(self):
        done = subprocess.run([sys.executable, str(ROOT / "chief_of_stuff.py"), "pipeline", "--help"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--id", done.stdout)
        self.assertIn("--ref", done.stdout)


class ForgePipelineTest(unittest.TestCase):
    def test_gitlab_id_reads_the_configured_host_and_credential(self):
        with mock.patch.object(backlog, "_call", return_value=(gitlab_row(), {}, "")) as call:
            got = forge.Forge(GL, token="test-token").pipeline(71)
        call.assert_called_once_with("GET", GL.api() + "/pipelines/71", "test-token", backlog.TIMEOUT)
        self.assertEqual(got, forge.Pipeline(71, "main", SHA, "passed", gitlab_row()["web_url"]))
        with self.assertRaises(FrozenInstanceError):
            got.status = "failed"

    def test_gitlab_reuses_review_statuses(self):
        for raw, expected in (("failed", "failed"), ("canceled", "failed"), ("pending", "pending"),
                              ("manual", "manual"), ("skipped", "skipped"), ("running", "running")):
            with self.subTest(raw=raw), mock.patch.object(
                    backlog, "_call", return_value=(gitlab_row(status=raw), {}, "")):
                self.assertEqual(forge.Forge(GL, token="test-token").pipeline(71).status, expected)

    def test_github_id_reads_an_actions_run_and_reuses_review_statuses(self):
        for status, conclusion, expected in (("completed", "success", "passed"),
                ("completed", "failure", "failed"), ("completed", "cancelled", "failed"),
                ("completed", "skipped", "passed"), ("in_progress", None, "running"),
                ("queued", None, "running")):
            gh = mock.Mock(return_value=(0, json.dumps(github_row(status=status, conclusion=conclusion)), ""))
            with self.subTest(status=status, conclusion=conclusion):
                got = forge.Forge(GH, gh=gh).pipeline(71)
                self.assertEqual(got, forge.Pipeline(71, "main", SHA, expected, github_row()["html_url"]))
                gh.assert_called_once_with(["api", "repos/example/app/actions/runs/71"])

    def test_branch_reads_head_and_latest_pipeline_for_that_head_on_both_forges(self):
        for github in (False, True):
            with self.subTest(github=github):
                ref = "release/a&b"
                row = github_row(head_branch=ref) if github else gitlab_row(ref=ref)
                branch = {"commit": {"sha" if github else "id": SHA}}
                listed = {"workflow_runs": [row]} if github else [row]
                urls = []

                def read(url):
                    urls.append(url)
                    return branch if len(urls) == 1 else listed

                gh = lambda args: (0, json.dumps(read(args[1])), "")
                call = lambda method, url, token, timeout: (read(url), {}, "")
                with mock.patch.object(backlog, "_call", side_effect=call):
                    got = forge.Forge(GH if github else GL, gh=gh, token="test-token").branch_head(ref)
                self.assertEqual(got.sha, SHA)
                self.assertEqual(got.ref, ref)
                self.assertEqual(got.id, 71)
                self.assertEqual(got.status, "passed")
                self.assertTrue(urls[0].endswith("/branches/release%2Fa%26b"), urls)
                if not github:
                    self.assertTrue(all(u.startswith(GL.api()) for u in urls))
                query = parse_qs(urlsplit(urls[1]).query)
                self.assertEqual(query["branch" if github else "ref"], [ref])
                self.assertEqual(query["head_sha" if github else "sha"], [SHA])
                self.assertEqual(query["per_page"], ["1"])
                if not github:
                    self.assertEqual(query["order_by"], ["id"])
                    self.assertEqual(query["sort"], ["desc"])

    def test_branch_without_a_pipeline_keeps_its_head(self):
        for home, branch, listed in ((GH, {"commit": {"sha": SHA}}, {"workflow_runs": []}),
                                    (GL, {"commit": {"id": SHA}}, [])):
            with self.subTest(home=home):
                gh = mock.Mock(side_effect=[(0, json.dumps(x), "") for x in (branch, listed)])
                with mock.patch.object(backlog, "_call", side_effect=[(branch, {}, ""), (listed, {}, "")]):
                    got = forge.Forge(home, gh=gh, token="test-token").branch_head("main")
                self.assertEqual(got, forge.Pipeline(None, "main", SHA, "none", ""))

    def test_read_errors_and_malformed_payloads_are_forge_errors(self):
        for home in (GL, GH):
            for payload, error in ((None, "HTTP 404"), ([], ""), ({}, ""),
                                   ({"id": 71, "status": "success"}, ""),
                                   (gitlab_row(status=None) if home == GL else github_row(status=None), "")):
                with self.subTest(home=home, payload=payload, error=error):
                    gh = mock.Mock(return_value=(1 if error else 0, json.dumps(payload), error))
                    with mock.patch.object(backlog, "_call", return_value=(payload, {}, error)):
                        with self.assertRaises(forge.ForgeError):
                            forge.Forge(home, gh=gh, token="test-token").pipeline(71)
        with self.assertRaises(forge.ForgeError):
            forge.Forge(GH, gh=lambda args: (0, "not JSON", "")).pipeline(71)

    def test_unreadable_branch_or_pipeline_list_is_not_no_pipeline(self):
        for home in (GL, GH):
            branch = {"commit": {"sha" if home == GH else "id": SHA}}
            for rows in (({},), (branch, {}), (branch, None)):
                with self.subTest(home=home, rows=rows):
                    gh = mock.Mock(side_effect=[(0, json.dumps(x), "") for x in rows])
                    with mock.patch.object(backlog, "_call", side_effect=[(x, {}, "") for x in rows]):
                        with self.assertRaises(forge.ForgeError):
                            forge.Forge(home, gh=gh, token="test-token").branch_head("main")


if __name__ == "__main__":
    unittest.main()
