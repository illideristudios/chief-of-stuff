# Plan: document `chief-of-stuff pipeline` (#845)

Help only. `chief-of-stuff pipeline --help` becomes the reference an agent trusts. No change to forge reads, status mapping, exit codes, or TOON fields.

Checked against `origin/develop` at `4417a8fe` (2026-10-20). This plan branch is `origin/main` at `50091f80` and does not contain `scripts/pipeline_cli.py`. Apply the edit on a tree that has that file, and re-read `main` first.

Standard: #838 (`docs/plans/issue-838.md` at `cd341b92`). Headings: `Purpose`, `When not to use`, `Options`, `Output`, `Exit codes`, `Side effects`, `Examples`. If the checker's spellings differ, match the checker.

## What `--help` says today

`ArgumentParser(description=__doc__)` prints the module docstring: one purpose line, then that GitHub branch status is an aggregate of every workflow's latest run and that ID and URL name the newest run, and that GitLab branches report the latest pipeline. `main`'s docstring states the exit codes; argparse does not show it. Flags are required `--root` (path), and a required mutually exclusive `--id` (int) or `--ref` (string). `--id` must be a positive integer (`ap.error` after parse). There is no epilog, no `prog`, and no formatter, so usage says `pipeline_cli.py`.

`evals/test_pipeline.py` `test_installed_command_routes_to_pipeline_help` requires the help text to contain `--id`, `--ref`, `aggregate`, `every workflow`, and `ID and URL`. That file is not in this issue's write scope. Keep those phrases.

## Help text

`prog="chief-of-stuff pipeline"`, `RawDescriptionHelpFormatter`. Purpose stays the argparse description (one or two sentences). The other prose sections go in the epilog so the headings stay on their own lines. Options stay argparse's own list. Each flag `help=` states type, default, and what it changes.

**Purpose.** Read one pipeline, or one branch head's CI, from the workspace's `Backlog:` forge. Use it when the question is that pipeline's status. A failed status is still a successful read.

**When not to use it.** To decide whether an open pull or merge request is ready to merge, use `merge-ready`. That verdict already includes the head pipeline plus conflicts, draft, review threads, and a stale pipeline sha. This command does not.

**Options.**

| Option | Type | Default | What it changes |
|---|---|---|---|
| `--root` | path | none (required) | Workspace whose `CLAUDE.md` `Backlog:` line selects the forge and repository. No `--repo` flag. |
| `--id` | int | none | One GitLab pipeline or one GitHub Actions run. Mutually exclusive with `--ref`; one of the two is required. Must be `> 0`. Does not aggregate. |
| `--ref` | string | none | Branch name. GitHub: status is the aggregate of every workflow's latest run at that head; ID and URL identify the newest run, whose own status may differ. GitLab: the latest pipeline at that head (one pipeline, so id, url, and status agree). Mutually exclusive with `--id`. |
| `-h`, `--help` | flag | off | Prints this text and exits 0. |

**Output.** TOON on stdout, one object, fields in this order: `id` (int, or `null`), `ref` (string), `sha` (string), `status` (string), `url` (string, `""` when there is no pipeline). Stderr is empty on success.

No pipeline (`--ref` and the head has no runs): exit 0, `id: null`, `status: none`, `url: ""`. A missing `--id` is not this result; it is a read error.

GitHub status, for the single `--id` run and for the `--ref` aggregate: `passed` when every included check completed with success, neutral, or skipped; `failed` when any included check completed with another conclusion; `running` when none failed and at least one has not completed. On `--ref`, a truncated workflow list is a read error, not a partial aggregate.

GitLab status, for `--id` and for the latest `--ref` pipeline: `success` → `passed`; `failed` and `canceled` → `failed`; `skipped`, `pending`, `created`, `waiting_for_resource`, `waiting_for_callback`, `preparing`, `scheduled`, `manual`, `draft`, `canceling` stay as those words; any other word → `running`.

Stderr: `pipeline: …` on a read or config error, and argparse's usage text on a usage fault. A usage fault prints no TOON.

**Exit codes.**

- `0`: the read succeeded, including `status: failed` and `status: none`; `--help`.
- `1`: config or forge read failed (no `Backlog:` line, missing GitLab credential, HTTP error, unreadable payload, truncated GitHub run list, branch name that is blank, untrimmed, `.`, or `..`). Stdout empty. The bad `--ref` cases are exit 1, not 2: argparse accepts the string and `branch_head` rejects it before any forge call.
- `2`: usage. Missing `--root`, missing both selectors, both `--id` and `--ref`, a non-integer `--id`, or `--id` that is not positive.

**Side effects.** Reads only. No dry run and no `--commit`, because nothing is written.

Reads: `CLAUDE.md` (the `Backlog:` line), then the forge. GitHub uses `gh api`. GitLab uses REST on that line's host, including a self-hosted host, with the credential `backlog.token` already resolves (not a flag). `--id` reads `actions/runs/<id>` or `pipelines/<id>`. `--ref` reads the branch, then that head's runs.

Writes: none. Not the store, not the board cache, not the worktree, not the forge.

**Examples.** Invented names only (§11). Copy-paste:

```
chief-of-stuff pipeline --root ~/cedar-desk --id 71
chief-of-stuff pipeline --root ~/cedar-desk --ref main
chief-of-stuff pipeline --root ~/cedar-desk --ref release/ledger
```

## Test

One method on `evals/test_cli.py`. Capture `chief-of-stuff pipeline --help` (`-h` exits 0, stderr empty) and call #838's shared check (`missing_sections`, and add `pipeline` to `ENFORCED`). Do not add a second section checker. If that helper is not in the tree, stop and report blocked. #838 also writes `evals/test_cli.py`.

The same method asserts substrings the shared check will not know: `--id`, `--ref`, `aggregate`, `every workflow`, `ID and URL`, `latest`, `merge-ready`, `status: none` (or `null` together with `none`). Those first five keep `evals/test_pipeline.py` green.

Run `python3 -m unittest evals.test_cli evals.test_pipeline`. `require_green_tests` needs passed > 0 and failed, skipped, and xfailed all zero.

## Files

- Write `scripts/pipeline_cli.py`: parser `prog`, formatter, description, epilog, and option `help=` only.
- Write `evals/test_cli.py`: the one test, plus the `ENFORCED` entry.
- Read `scripts/forge.py`: `Pipeline`, `pipeline`, `branch_head`. Do not edit.
- Read `scripts/forge_review.py`: `github_pipeline`, `gitlab_pipeline_status`. Do not edit.
- Read `chief_of_stuff.py`: the `pipeline` route. Do not edit (#838 owns top-level help).
- Read `evals/test_pipeline.py`: the help phrases above. Do not edit.

## Out of scope

Behavior, status words, exit codes, a `--repo` flag, a dry run, `--commit`, `docs/commands.md`, `README.md`. If #838's docs freshness test is already on, this help change makes `docs/commands.md` stale. Regenerating it is #838's `docs` command and that file is not in this issue's ownership: report that, and do not hand-edit the file.

## Risks

- **Base tree.** Implementing on this plan branch creates a command that does not exist here. The behavior above is `origin/develop` at `4417a8fe`. Re-read `main` before editing; if the exit codes or the no-pipeline shape differ, match the code.
- **Shared check absent.** `missing_sections` is not on `develop` as of `4417a8fe`. The test waits for #838. A private copy of the headings would diverge.
- **`test_cli.py` conflict.** Every per-command issue adds one name to `ENFORCED`. Keep the helper; add `pipeline`.
- **Exit 1 versus 2 for a bad `--ref`.** Documenting it as a usage fault would be false. Argparse does not reject it.
- **GitHub id versus aggregate.** Help that says `--ref` status is the newest run's status would be false. ID and URL are that run; status is the aggregate.

## Logic

```
H := --help matches pipeline_cli.main and forge.Pipeline on develop
N := a head with no runs is id null, status none, exit 0
M := a missing --id is that same result
C := the test calls #838's checker
D := a second checker is added here
R := a bad --ref (blank, untrimmed, "." , "..") exits 2

□(N ∧ ¬M)                 [empty --ref list versus ForgeError on --id]
□(C ∧ ¬D)                 [acceptance: the shared check]
¬R                        [argparse accepts the string; branch_head returns 1]
∴ document none only for --ref; wait for #838's helper; call bad refs exit 1
```
