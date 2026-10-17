# chief-of-stuff architecture

This is the canonical architecture document. The README is the user's guide and is not this document. Every section is numbered, numbers are never reused, and a review grades the code against these sections. The sections state the required architecture, not the present state of the code. Where the code does not yet meet a section, the gap is a row in `compliance.md`, in this directory.

## 1. Style

The project follows Clean Architecture as Robert C. Martin defines it, and SOLID. The circles, from the centre outward, are Entities, Use Cases, Interface Adapters, and Frameworks and Drivers. The dependency rule: source code dependencies point inward only, so nothing in an inner circle names anything in an outer one. Single responsibility, open-closed, Liskov substitution, interface segregation and dependency inversion are the five principles a review holds each module to.

YAGNI and DRY limit both. An interface, factory or layer that no caller and no section of this document needs today is not added, and a copy of what already exists is not made. Where a section here and the code disagree, the section stands and the code is recorded as a gap in `compliance.md`.

## 2. Skills

A skill is loaded on demand on every host. The agent rules name the skill and the moment to read it, and the host reads the skill's file then. No host receives the text of a skill in its starting prompt.

## 3. Page modules

The shared helpers the page modules use (the stylesheet head, the file write that skips unchanged content, the day's context, the pending count, the duration span, and the markdown and section renderers) live in one module of their own and are public names. `decision_page.py`, `issue_page.py`, `source_page.py` and `render_board.py` import them from there. No page module imports another page module's underscore-prefixed name.

## 4. Circles

Each script under `scripts/`, the two launchers and each hook file belong to one circle. A module that does the work of two circles is a gap against this section until it is split. `inbox.py`, `audit_tasks.py` and `flow.py` are placed by their parts, as section 8 describes.

The owner decisions in the 1.0 ledger (`docs/specs/2026-10-09-chief-of-stuff-1.0-ledger.md`, section "Owner decisions", Zach, 2026-10-10) amend this document. Where a section here disagrees with one of them, the owner decision stands and the section is rewritten to match. A module the ledger retires, such as the file mailbox, the tracker parser and its command lines, the flow tick, the notify service, the terminal launchers or the non-Claude watcher, keeps its circle in this table until it is deleted. A gap against a retiring module closes when the module is deleted.

New work goes on the engine path: the store, the forge adapter, the model port, the reconcile pass and the pages built on them. The goal is 1.0, then 1.1, with as much legacy dropped as possible (Zach, 2026-10-16), so getting the engine to run the workspace comes first. A retiring module takes only the fixes today's loop needs to keep running until that cutover. A new behaviour lands on the engine path. It also lands on a retiring module only when the loop needs it before the cutover, and then the engine side is in the same change or filed as its own issue. A change that adds behaviour only to a retiring module is a gap against this section.

| Circle | Modules |
|---|---|
| Entities | `tracker.py`, `md.py`, `clock.py`, `runtimes.py`, `policy.py`, `findings.py`, `ownership.py`, `orphans.py`, `estimate.py`, `tracker_log.py`, `tree_state.py`, the finding types of `audit_tasks.py`, the message format of `inbox.py`, `guard_commands.py` (the worker guard's pure shell-command judgements), `limits.py` (runtime limit readings and their judgements, section 10) |
| Use Cases | the ownership rules and judgements of `audit_tasks.py`, `dispatch_prompt.py`, `one_shot.py`, `kanban.py`, `merge_ready.py`, `merge_approved.py`, `review_threads.py`, `task_forge.py`, `model_port.py` (the model port), `issue_gate.py` (issue first, then one store row), `trio_gate.py` (the decision trio, design 5a), `engine_merge.py` (executes a planned merge, logged on the issue), `forge_port.py` (`ForgeError`, the forge failure boundary), `reconcile.py` (the engine's planner: what the state calls for), `guard_rules.py` (the worker guard's Stop, permission-relay and session-start rules), the ready-task and stagnation judgements of `flow.py`, `performance.py` |
| Interface Adapters, presenters | `fragment.py`, `columns.py`, `panels.py`, `gantt.py`, `flow_chart.py`, `render_board.py`, `decision_page.py`, `issue_page.py`, `source_page.py`, `module_graph.py`, `workers_page.py`, `page_reload.py`, and the page module of section 3 |
| Interface Adapters, gateways | `store.py` (the issue-keyed store behind the ORM, and its `migrations/`), `forge.py` (the one forge adapter for GitHub and GitLab), `backlog.py`, `backlog_ref.py`, `board_sources.py`, `workspace.py`, `settings.py`, `tracker_read.py`, `tracker_write.py`, `forge_review.py`, `one_shot_report.py`, `tree_claims.py`, `slots.py` (the worker concurrency cap, E21), `limits_history.py` (the account-wide limit readings behind the ORM, section 10), the git and forge fact gathering of `audit_tasks.py`, the mailbox storage of `inbox.py` |
| Frameworks and Drivers | `claude_adapter.py` (the only module that spawns a host CLI), `git_trees.py`, `git_view.py`, the command line of `inbox.py`, the audit orchestration and command line of `audit_tasks.py`, `notify.py`, `notify_service.py`, `supervisor.py` (the engine venv and LaunchAgent), `startup.py` (the engine and `work` commands' workspace settings and forge), `store_access.py` (the migrated store, under the migrate lock), `kanban_cli.py` (the `kanban` command line), `pipeline_cli.py` (the `pipeline` command line: a pipeline's status, or a branch's head and latest pipeline, read through `forge.Forge`), `process_status.py`, `session_exec.py`, `spawn_session.py`, `probe_health.py`, `pages.py`, `chief_of_stuff.py`, `start_coordinator.py`, `hooks/hooks.json`, `board_guard.py`, `init_workspace.py`, `install_model_guidance.py`, `make_worktree.py`, `migrate_backlog.py`, `one_shot_result.py`, `shell_setup.py` (the login shell, and the only place the workspace's `[workers] teardown` and `teardown_list` commands run; Use Cases receive teardown as a passed-in function, never by import), `cleanup.py` (engine-owned worktree removal behind every finding-27 guard, E19; it runs the workspace teardown in a tree before its container-mount guard), `engine.py` (the workspace singleton loop that executes the reconcile plan), `work.py` (the issue-first intake command line), `worker.py` (worker launch through the adapter and completion collection), `worker_guard.py` (the worker guard's hook entry point), `guard_binding.py` (the worker's binding read from its environment), `engine_mcp.py` (the engine's report and ask MCP server), `guard/hooks/hooks.json`, `adopt_one_shots.py`, `sync_page_theme.py`, the command line and tick orchestration of `flow.py`, `performance_report.py`, `limits_probe.py` (unbilled reads of each runtime's limits, section 10), `limits_cli.py` (the `limits` command line) |

## 5. Enforcement

The dependency rule of section 1 is enforced by `evals/test_architecture.py`. It holds the circle table of section 4 and fails on any import from an inner circle to an outer one. Imports that violate the rule today are listed in that test by name, the list may only shrink, and each entry is also a gap in `compliance.md`.
## 6. Reviewers

Review is policy, as the 1.0 ledger settles (D7, F10, F12, F13; plan M4). Personas (role, model, effort, rules), rooms, stages, and the review policy are data: `assets/policy.toml`, read and validated by `policy.py` (Entities). Which personas review a stage, what counts as an open finding, the retry limits, and when the architect is consulted live there, with defaults per persona that a stage or room overrides.

The engine runs the review panel.
- `reconcile.py` (Use Cases) plans one review launch for each persona the policy names for the stage, and they run in parallel.
- It plans a fix for every open finding, so review burns down to none.
- It routes findings to the architect when the policy says to: the architect disposes of each one as fix, file, keep or discard.
- It pings after three fix rounds.
- `engine.py` executes the plan through the model port.

A review is bound to the head it reviewed: the segment records the reviewed commit, and a merge happens only when that commit is the change's current head.

What a reviewer does inside its own run is its persona's rules. The default reviewer persona runs the `extras:code-review` skill and does not fix. chief-of-stuff does not reimplement that skill's review. Findings come back in one review report shape (issue #563) and are kept as store records.

Until the engine path replaces it, the legacy one-shot path keeps its `Review:` line: `dispatch_prompt.compose` adds it to a Tasks row whose `stage` cell is `review`.

## 7. Assignments

An implementer's assignment tells the worker about the architecture: it names the canonical architecture document of the workspace the task belongs to, and the worker reads it before changing code. The work is held to that document.

The path is read from the `Canonical document:` bullet of the workspace's own `## Architecture` block, the first backticked path in it, resolved against the workspace root. `dispatch_prompt.compose` writes it as an `Architecture:` line in both the one-shot and the interactive assignment. A workspace without that block or bullet gets the assignment unchanged and no refusal.

## 8. Single responsibility

A module has one reason to change. `inbox.py` and `audit_tasks.py`, the two largest, and `flow.py` each carry more than one. The 1.0 ledger retires the mailbox and the flow tick (G1, E1, E2) and rewrites the task audit (F5), so these modules are not split for their own sake. The parts the engine carries, such as the ready predicate, the stagnation rule and the audit's judgements, land in the circles below as they move. The rest goes when the module is deleted.

| Module today | Responsibility | Circle |
|---|---|---|
| `inbox.py` | The message format and its validation rules | Entities |
| `inbox.py` | The mailbox storage: send, list, read, ack, drain and wait | Interface Adapters, gateways |
| `inbox.py` | The command line | Frameworks and Drivers |
| `audit_tasks.py` | The finding types | Entities |
| `audit_tasks.py` | The ownership rules and the pure judgements | Use Cases |
| `audit_tasks.py` | Gathering facts from git and the forge | Interface Adapters, gateways |
| `audit_tasks.py` | The audit orchestration and the command line | Frameworks and Drivers |
| `flow.py` | The ready-task and stagnation judgements | Use Cases |
| `flow.py` | The tick orchestration and the command line | Frameworks and Drivers |

Each module keeps re-exporting the names its callers import until they import from the new modules. Where the module boundaries and their names fall is decided when the work is assigned.

## 9. Agent performance

Agent performance is measured from the store's stage segments, as the 1.0 ledger's owner decision A3 settles: a query over segments, not a parser of Log lines. A run is one segment, keyed (issue, stage, attempt), with its start and end times and its outcome. The engine records the segment when it launches a worker, through `store.record_segment` with the runtime, model, effort and agent type it launched with, and closes it with the outcome when it collects the worker. The tracker Log, the one-shot reports and interactive sessions are not sources.

The agent type follows the declaration rule: a segment records an agent type only when the workspace's `Agent:` lines declare the type the worker was launched as, checked with `make_worktree.agent_types`. Without that declaration the default agent runs and the segment records no type. Segments recorded before this have no runtime fields.

Segment times are full timestamps, so a run that ends after midnight keeps its end, and no daylight-saving rule is needed.

| Boundary | Signature | Circle |
|---|---|---|
| `store.Segment` | `issue_key: str, stage: str, attempt: int, started_at: str, ended_at: str \| None, outcome: str \| None, runtime: str, model: str, effort: str, agent_type: str` | Interface Adapters, gateways |
| `performance.Row` | `period: date, runtime: str, model: str, effort: str, agent_type: str, runs: int, completed: int, human_review: int, relaunched: int, median_minutes: int \| None` | Use Cases |
| `performance.table` | `(segments: Iterable[Segment], since: date, until: date, by: str, zone: ZoneInfo) -> list[Row]`, where `by` is `day` or `week` | Use Cases |
| `performance_report.main` | `(argv: list[str] \| None = None) -> int` | Frameworks and Drivers |

For each day or week and each key of runtime, model, effort and agent type, the report counts the runs started, completed, sent to human review and relaunched, and the median whole minutes from start to end, taking the lower middle value for an even count. A run belongs to the period of its start, in the workspace zone. A week starts on Monday, and `Row.period` is the period's first day: the day itself, or the week's Monday. `--since` and `--until` are dates in the workspace zone and both are inclusive; `--until` defaults to today and `--by` to `day`. A segment with no model, effort or type is keyed by what is there, and an empty key field prints as `-`. `performance.py` reads no file. `performance_report.py` reads the segments through `store_access`, calls `performance.table` and prints one table. It is the `performance` entry of `COMMANDS` in `chief_of_stuff.py`: `chief-of-stuff performance --since DATE [--until DATE] [--by day|week]`.

A page may show the same measures over time: each runtime, model and effort pair's trend and success rate per kind of task, with its sample size. The scoring is in `performance.py` (Use Cases), over segments passed in. Review rounds are the count of review-stage attempts per issue, the kind of task is the issue's workflow, and the pipeline result is read through the forge adapter, the source of truth. A presenter renders the page from rows handed to it and reads no file. Scoring makes no billed call and never blocks a launch.

Review findings are not counted until review reports have one shape (issue #563). Then findings are joined to runs by the issue key.

## 10. Runtime limits

Each runtime's limits are read without a billed call, recorded over time, and shown on the board: rolling windows, weekly caps and model caps, for claude, codex, cursor and agy. A reading is one pass over every runtime the workspace can launch, each under the account it launches with: `limits_probe.read` is called once per runtime and account, and the results are recorded together. A limit that cannot be read is recorded as unknown with the reason, never as zero.

`limits.py` (Entities) holds the reading record and the pure judgements over readings: the burn rate across a series of readings, and whether a limit is near its cap before its reset. A limit is near its cap when its used share, or the share its burn rate projects at its reset, reaches `warn_at`: the `[limits] warn_at` setting, a fraction between 0 and 1 that defaults to 0.8 and is validated by `settings.py`. It reads no file. `limits_probe.py` (Frameworks and Drivers) takes the reading, one function per runtime, only from sources that cost nothing: the runtime's local state or logs, or a status command that starts no model session. It never goes through `claude_adapter.py`, whose calls are billed and which serves the model port. An existing tool that already reads a runtime's usage is preferred to a new parser, and adopting one needs the owner's approval like any other dependency.

Limits belong to an account, not a workspace, so readings are account-wide: two workspaces on one account share one history, and two accounts on one machine never mix. A reading's account is the runtime's account as the workspace launches it: the `[workers] claude_as_profile` for claude, empty for a runtime's default login. The probe reads that account's limits, and every history query names the account it wants. `limits_history.py` (Interface Adapters, gateways) keeps the readings behind the ORM, in their own SQLite database at `$XDG_STATE_HOME/chief-of-stuff/limits.sqlite` (default `~/.local/state/chief-of-stuff/limits.sqlite`), with migrations of their own. It reuses the store's migration setup through a shared helper rather than a copy. `store.py` stays the issue-keyed workspace store and holds no readings. Section 9's single source applies to agent performance and is not changed by this store.

`limits_cli.py` is the `limits` entry of `COMMANDS` in `chief_of_stuff.py`: `chief-of-stuff limits` takes one reading of every runtime, records it, and prints it. Readings are taken on a schedule by the engine's pass: each pass takes one reading, one `limits_probe.read` per runtime and account, and records it through `limits_history`, and a failed reading never stops the pass. The board reads the history and judges it through `limits.py`. It never probes a runtime itself. The point-in-time query that chooses a dispatch runtime reads the same history.

| Boundary | Signature | Circle |
|---|---|---|
| `limits.Reading` | `runtime: str, account: str, limit: str, used: float \| None, cap: float \| None, unit: str, resets_at: datetime \| None, read_at: datetime, unknown: str` | Entities |
| `limits.burn_rate` | `(readings: list[Reading]) -> float \| None` | Entities |
| `limits.near_cap` | `(readings: list[Reading], now: datetime, warn_at: float) -> bool` | Entities |
| `limits_probe.read` | `(runtime: str, account: str) -> list[Reading]` | Frameworks and Drivers |
| `limits_history.LimitsHistory.record` | `(readings: list[Reading]) -> None` | Interface Adapters, gateways |
| `limits_history.LimitsHistory.readings` | `(runtime: str, account: str, since: datetime) -> list[Reading]` | Interface Adapters, gateways |
| `limits_cli.main` | `(argv: list[str] \| None = None) -> int` | Frameworks and Drivers |

## 11. Stack independence

chief-of-stuff is independent of any deployment technology, hosting provider, container runtime, infrastructure tool and application stack. Its code, its agent rules, its skills and its eval fixtures name none of them, and no behaviour depends on one being installed.

What a workspace builds, deploys or runs, and how, reaches chief-of-stuff only through that workspace's own configuration: the `## Coordinator` block (its human-only actions, `Health:` lines and the rest) and the Settings TOML (such as `[workers] teardown` and `teardown_list`). chief-of-stuff runs those commands as given and never interprets them. A rule or a fixture that needs an example of a workspace-specific action uses an invented name, never a real product.

Two things are chief-of-stuff's subject, not a stack, and stay named: the host CLIs it coordinates (claude, codex, cursor and agy, in `runtimes.py`), and the forges it integrates with (GitHub and GitLab, behind `forge.py`). The repository's own CI tooling under `.github/` is how this project is tested, not something chief-of-stuff runs, and is outside this section.
