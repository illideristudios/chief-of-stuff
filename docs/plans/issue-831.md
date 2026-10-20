# Plan: prune the mailbox and its TOON transport (#831, M7.2 of #646)

Ledger rows: G1 (`inbox.py`, drop) and G2 (TOON, drop). This plan deletes the mailbox and narrows the vendored TOON codec to what surviving code still calls. It does not delete TOON as a whole, because ten surviving modules still emit or read it (see D1).

## Decisions

### D1. `toon_format` stays, narrowed; it is not removed

```
glossary
  R  := nothing that survives this issue still emits or reads TOON
  K  := keep scripts/_vendor/toon_format, narrowed to what the survivors call
  E  := tracker_read, kanban, board_sources, process_status, adopt_one_shots, one_shot_result, notify_service and chief_of_stuff.install_main encode TOON
  D  := one_shot, one_shot_report and start_coordinator.check_once decode TOON
  O  := the other #646 issues (kanban, tracker, notify, report format) delete those modules, each in its own issue
premises
  1. R → ¬K                                [issue #831: "remove ... if nothing that survives still emits TOON"]
  2. E ∧ D hold today                      [grep 'from _vendor.toon_format import' in those files, plus chief_of_stuff.py:19 and start_coordinator.py:20]
  3. O is not part of #831                 [issue #831 Remove list names inbox.py only; ledger G2 caveat: the report format is rewritten elsewhere]
  4. (E ∨ D) → ¬R                          [definition of R]
  5. ¬R → keep only what survivors need    [issue #831: "Otherwise keep only what the surviving output needs"]
derivation
  2, 4 ⊢ ¬R
  5    ⊢ narrow, do not delete
∴ keep toon_format and delete its unused part (utils.py, which needs tiktoken, and the count_tokens, estimate_savings and compare_formats exports). The vendor directory goes in the issue that removes the last caller.
```

Prose: the vendored codec is used by the worker-result report, `result`, `tracker`, `kanban`, `sources`, `processes`, `adopt`, `notify status` and `install`. Those are other issues, and deleting the vendor now would break all of them. The narrowing is only the benchmark helpers that nothing imports.

### D2. Workers lose the mailbox text, not just the script

```
glossary
  S  := a worker prompt or coordinator rule names inbox.py or `chief-of-stuff inbox`
  X  := the command still exists
  A  := a worker's ask or stop reaches the engine
premises
  1. (¬X ∧ S) → the worker follows an instruction that fails   [removing the COMMANDS entry makes `chief-of-stuff inbox` exit with an unknown command]
  2. a failed ask or stop is dropped                           [ledger G1: "a worker's ask or stop must reach the engine and must never be dropped"]
  3. the engine takes asks and reports through its tools       [issue #831 body: "Workers report to the engine through the report tool and the store"]
derivation
  1, 2 ⊢ (¬X ∧ S) → ¬A
  so S must go in the same change as X
∴ every prompt, rule and doc that names the mailbox is edited with the script. The replacement text points at the engine's ask and report tools.
```

Open point for the implementer to confirm with the coordinator before writing the replacement wording: the exact tool names a worker sees in an interactive session. Codex, Cursor and Antigravity interactive workers have only the mailbox today. The ledger decides Claude only for 1.0 (G5, "Decided"), so the plan removes their mailbox registration text and does not invent a new channel for them.

### D3. The tree has no `CIRCLES` or `OUTWARD_IMPORTS`

Neither exists in `evals/test_architecture.py` here; that is compliance row C1, still open. The only list `inbox` sits in today is `DRAWS_NOTHING` (`evals/test_architecture.py:36`, on the `migrate_backlog` line). Remove it there. If C1 lands first, also remove the `inbox` row from `CIRCLES` and any `inbox` entry from `OUTWARD_IMPORTS`, and add nothing to `OUTWARD_IMPORTS`.

## Gate: do not start until this holds

The issue says "when the engine dispatches this work live and nothing the loop or a workspace runs still calls it". Check, and put the output in the PR description.

1. The coordinator confirms the engine dispatches this repo's work live.
2. `grep -rIn -E "inbox\.py|chief-of-stuff inbox|\binbox\b" --exclude-dir=.git .` lists only the files this plan edits, `evals/results/PROGRESS.md` (history, left alone), `docs/specs/` (the ledger and plan, history) and the two architecture files (step 9).
3. No live workspace arms a `[Mailbox check]` cron: the coordinator reports its `CronList`. Installed releases are versioned and pinned (`CHIEF_OF_STUFF_RELEASE`), so a pinned older release keeps its own `inbox.py`; only a restart onto the new release loses it.
4. `.chief-of-stuff/mailbox/` is empty in each live workspace. A non-empty mailbox holds an unread ask or stop; send it to the engine by hand first.

If any of these fails, stop and report; do not delete.

## Steps, each its own small commit

1. **Failing test.** In `evals/test_architecture.py` add `REMOVED = ("inbox",)` and two tests in `DependencyRuleTest`, red while `inbox.py` exists. This is the "architecture test proves it" step.
   - `test_a_removed_script_is_gone_and_no_file_imports_it`: `scripts/<name>.py` does not exist, and `script(n)` over `imports(f)` for every `*.py` in `scripts/`, the repo root, `evals/` and `hooks/` never equals the name. It reuses the existing `imports` and `script` helpers.
   - `test_every_command_routes_to_a_script_that_exists`: `ast.literal_eval` the `COMMANDS` dict out of `chief_of_stuff.py`; each value is a file.
2. **Callers first.** Edit the code that calls the mailbox so nothing imports or runs it.
   - `scripts/dispatch_prompt.py`: delete `INBOX_SCRIPT`, `AGY`, `NON_CLAUDE_REGISTER`, `mailbox_check` and the `inbox_script` and `coord_mailbox` arguments. Rewrite the HEADER register, ask and stop sentences and `REPORT` to name the engine tools (D2). In `compose` drop the non-Claude registration splice and the `.replace(...)` chain that only reworded the mailbox sentences. Keep the Codex and Cursor plan gates.
   - `start_coordinator.py`: delete the `inbox.py` subprocess and the `inbox exit` early return in `check_once`, the `mailbox_check` string and its use in `host_schedule`, and the inbox words in the rewritten `## Check`, `## Sessions` and host-adapter text. Keep `toon_decode`, which still decodes `processes` output.
3. **Delete the module.** `git rm scripts/inbox.py`; remove `"inbox": "scripts/inbox.py"` from `COMMANDS` in `chief_of_stuff.py`; remove `"inbox"` from `DRAWS_NOTHING`.
4. **Delete its tests and cases.**
   - `git rm evals/test_inbox.py evals/test_inbox_adversarial.py evals/test_inbox_boundary_adversarial.py` (the ledger counts 86 tests).
   - `evals/run.py:68,71`: drop `"inbox.py"` from `SCRIPTS` and `"inbox"` from `OPERATIONS`.
   - Eval cases: delete `evals/cases/changed-check-says-only-the-change` and the `[Mailbox check]` turn of `silent-check-says-nothing` (keep its `[Scheduled check]` turn). In the other 12 cases that mention it, delete only the `inbox` alternation from the `before` regexes and the `.chief-of-stuff/mailbox/.gitignore` allowance (`dispatch-needs-yes`, `para-move`); `triage-acts-on-the-reply` drops its `--mailbox-dir` assertion. Read each case before editing; if an edit leaves a grader that can never fail, delete the grader.
   - Tests that pin mailbox text: `test_dispatch_prompt.py` (43 references), `test_runtime_launch.py` (`:64` check another installed script such as `scripts/dispatch_prompt.py`; `:99` and `:313` rewrite to the new text), `test_run.py` (`:531` use another pinned helper, `:998` drop `inbox.py`), `test_cases.py` (24 inbox grader tests, `:848` to `:1298` and `:2114`), `test_agent_budget.py` (11 pins on agent and skill sentences), `test_one_shot.py:2071` (comment only) and `test_spawn_session.py:684` (docstring). `test_backlog.py` uses `"inbox.py"` as an issue title in a fixture; leave it.
5. **Rules and docs stop describing it.**
   - `agents/chief-of-stuff.md` lines 12, 83, 84, 155-158, 183-184, 196: remove the mailbox-arming bullet, the `[Mailbox check]` handler, the `chief-of-stuff inbox list` calls and the inbox-delivered ask and stop handling.
   - `skills/coordinator-sessions/SKILL.md` lines 16, 19, 21 (dual transport, agy), `skills/dispatch/SKILL.md` lines 57, 61 and `skills/review-pipeline/SKILL.md` line 20.
   - `README.md` line 13 (drop `inbox` from the command list) and lines 90-98 (delete the mailbox check, `inbox wait`, `.toon` message and restart paragraphs; keep the sentence on the Claude 15-minute check and the non-Claude watcher, minus "inbox"). `docs/notify.md:29` is about `notify status` and stays.
6. **Narrow the vendor.** `git rm scripts/_vendor/toon_format/utils.py`; remove `count_tokens`, `estimate_savings` and `compare_formats` from `toon_format/__init__.py`; rewrite `scripts/_vendor/README.md` to drop "mailbox commands" and the `inbox.py` `[]` sentence. `evals/test_toon_string_escapes.py` and `evals/test_runtime_launch.py:55` stay unchanged and must still pass.
7. **Prove it.** Run the gate's grep again: no `inbox` hit outside `evals/results/PROGRESS.md`, `docs/specs/` and the two architecture files. Then `python3 -m unittest discover -s evals -p 'test_*.py'` is green.
8. **Version bump** if the repo's practice requires one for plugin changes (recent merges bump the patch version); follow the surrounding commits.
9. **Hand-off, not done here.** The architecture agent owns `architecture/ARCHITECTURE.md` and `compliance.md`. Ask it to remove the three `inbox.py` rows from the section 8 split table (lines 53-55), the `inbox.py` mentions on lines 25-29 and 49, and the `inbox.py` half of compliance row C6 (`audit_tasks.py` remains). The implementer sends that request and does not edit those files.

## Tests and acceptance mapping

| Acceptance | Proof |
|---|---|
| Module, `COMMANDS` entry, tests, eval cases, `CIRCLES` and `OUTWARD_IMPORTS` entries gone; `OUTWARD_IMPORTS` only shrinks | Steps 3 and 4; D3; the two new architecture tests |
| Nothing on the engine path imports a removed module | Step 1 test, plus the existing cycle and classification tests |
| README, `docs/`, skills no longer describe it | Step 5; the closing grep in step 7 |
| Keep list is empty | Nothing to keep untouched; the narrowed vendor is the only retained part (D1) |
| Full suite green | Step 7. The suite shrinks by the three deleted files; it needs `passed > 0` and `failed`, `skipped`, `xfailed` all zero. Take a baseline run before step 2 |

## Risks

- **Non-Claude interactive workers lose their only channel** (Codex, Cursor, Antigravity). The ledger accepts it (Claude only for 1.0). Pinned older releases are unaffected.
- **A wrong replacement tool name** in the worker prompt would make asks fail silently. Confirm the names from a live engine-dispatched worker's tool list before step 2.
- **Over-deleting graders.** Fourteen cases mention the mailbox and most only forbid a send. Deleting a grader that tested something else (for example IPC-before-anything in `brief-a-session`) loses coverage. Edit the regex, not the whole grader, unless it can never fail.
- **Narrowing the vendor too far.** `encoders.py`, `decoder.py`, `normalize.py`, `writer.py`, `types.py`, `constants.py`, `logging_config.py` and the `_*` helpers are all reached from `encode` and `decode`; only `utils.py` is not. `test_toon_string_escapes.py` and the round-trip tests guard this.
- **Parallel edits.** `start_coordinator.py`, `dispatch_prompt.py`, `agents/chief-of-stuff.md` and `README.md` are touched by other #646 issues. Rebase before each commit; the edits are deletions, so conflicts are line-local.

## Proposed file ownership

write:
- `scripts/inbox.py` (delete)
- `scripts/_vendor/toon_format/utils.py` (delete)
- `scripts/_vendor/toon_format/__init__.py`
- `scripts/_vendor/README.md`
- `chief_of_stuff.py`
- `start_coordinator.py`
- `scripts/dispatch_prompt.py`
- `agents/chief-of-stuff.md`
- `skills/coordinator-sessions/SKILL.md`, `skills/dispatch/SKILL.md`, `skills/review-pipeline/SKILL.md`
- `README.md`
- `evals/test_inbox.py`, `evals/test_inbox_adversarial.py`, `evals/test_inbox_boundary_adversarial.py` (delete)
- `evals/test_architecture.py`, `evals/run.py`, `evals/test_dispatch_prompt.py`, `evals/test_runtime_launch.py`, `evals/test_run.py`, `evals/test_cases.py`, `evals/test_agent_budget.py`, `evals/test_one_shot.py`, `evals/test_spawn_session.py`
- `evals/cases/` (the 14 cases named in step 4)

read:
- `scripts/_vendor/toon_format/` (everything else)
- `architecture/ARCHITECTURE.md` and `architecture/compliance.md` (owned by the architecture agent; change requested, not made)
- `docs/specs/2026-10-09-chief-of-stuff-1.0-ledger.md`, `docs/notify.md`, `evals/results/PROGRESS.md`
- `scripts/process_status.py`, `scripts/one_shot.py`, `scripts/tracker_read.py` and the other TOON callers

The issue's own list names `scripts/inbox.py`, `scripts/_vendor/toon_format/`, `chief_of_stuff.py` and `evals/`. This plan adds `start_coordinator.py`, `scripts/dispatch_prompt.py`, `agents/`, `skills/` and `README.md` because they call or describe the mailbox; without them the deletion leaves live instructions to a missing command (D2).

## Skipped

No baseline test run at plan time: the command guard blocked a background run, so the first implementation step takes it. The gate depends on the coordinator's word that the engine dispatches live; it was not checked from here.
