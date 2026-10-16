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

Each script under `scripts/`, the two launchers and each hook file belong to one circle. A module that does the work of two circles is a gap against this section until it is split. `inbox.py`, `audit_tasks.py` and `flow.py` are placed by the parts section 8 splits them into; each is a gap until the split lands.

| Circle | Modules |
|---|---|
| Entities | `tracker.py`, `md.py`, `clock.py`, `runtimes.py`, `policy.py`, `findings.py`, `ownership.py`, `orphans.py`, `estimate.py`, `tracker_log.py`, `tree_state.py`, the finding types of `audit_tasks.py`, the message format of `inbox.py`, `guard_commands.py` (the worker guard's pure shell-command judgements), `limits.py` (runtime limit readings and their judgements, section 10) |
| Use Cases | the ownership rules and judgements of `audit_tasks.py`, `dispatch_prompt.py`, `one_shot.py`, `kanban.py`, `merge_ready.py`, `merge_approved.py`, `review_threads.py`, `task_forge.py`, `model_port.py` (the model port), `issue_gate.py` (issue first, then one store row), `trio_gate.py` (the decision trio, design 5a), `engine_merge.py` (executes a planned merge, logged on the issue), `forge_port.py` (`ForgeError`, the forge failure boundary), `reconcile.py` (the engine's planner: what the state calls for), `guard_rules.py` (the worker guard's Stop, permission-relay and session-start rules), the ready-task and stagnation judgements of `flow.py`, `performance.py` |
| Interface Adapters, presenters | `fragment.py`, `columns.py`, `panels.py`, `gantt.py`, `flow_chart.py`, `render_board.py`, `decision_page.py`, `issue_page.py`, `source_page.py`, `module_graph.py`, `workers_page.py`, `page_reload.py`, and the page module of section 3 |
| Interface Adapters, gateways | `store.py` (the issue-keyed store behind the ORM, and its `migrations/`), `forge.py` (the one forge adapter for GitHub and GitLab), `backlog.py`, `backlog_ref.py`, `board_sources.py`, `workspace.py`, `settings.py`, `tracker_read.py`, `tracker_write.py`, `forge_review.py`, `one_shot_report.py`, `tree_claims.py`, `slots.py` (the worker concurrency cap, E21), `limits_history.py` (the account-wide limit readings behind the ORM, section 10), the git and forge fact gathering of `audit_tasks.py`, the mailbox storage of `inbox.py` |
| Frameworks and Drivers | `claude_adapter.py` (the only module that spawns a host CLI), `git_trees.py`, `git_view.py`, the command line of `inbox.py`, the audit orchestration and command line of `audit_tasks.py`, `notify.py`, `notify_service.py`, `supervisor.py` (the engine venv and LaunchAgent), `startup.py` (the engine and `work` commands' workspace settings and forge), `store_access.py` (the migrated store, under the migrate lock), `kanban_cli.py` (the `kanban` command line), `process_status.py`, `session_exec.py`, `spawn_session.py`, `probe_health.py`, `pages.py`, `chief_of_stuff.py`, `start_coordinator.py`, `hooks/hooks.json`, `board_guard.py`, `init_workspace.py`, `install_model_guidance.py`, `make_worktree.py`, `migrate_backlog.py`, `one_shot_result.py`, `shell_setup.py`, `cleanup.py` (engine-owned worktree removal behind every finding-27 guard, E19), `engine.py` (the workspace singleton loop that executes the reconcile plan), `work.py` (the issue-first intake command line), `worker.py` (worker launch through the adapter and completion collection), `worker_guard.py` (the worker guard's hook entry point), `guard_binding.py` (the worker's binding read from its environment), `engine_mcp.py` (the engine's report and ask MCP server), `guard/hooks/hooks.json`, `adopt_one_shots.py`, `sync_page_theme.py`, the command line and tick orchestration of `flow.py`, `performance_report.py`, `limits_probe.py` (unbilled reads of each runtime's limits, section 10), `limits_cli.py` (the `limits` command line) |

## 5. Enforcement

The dependency rule of section 1 is enforced by `evals/test_architecture.py`. It holds the circle table of section 4 and fails on any import from an inner circle to an outer one. Imports that violate the rule today are listed in that test by name, the list may only shrink, and each entry is also a gap in `compliance.md`.
## 6. Reviewers

chief-of-stuff stores no persona text and no review panel logic. A review is done by running the `extras:code-review` skill, which chooses the personas, including the Clean Architecture expert for the style section 1 declares. This is the current choice and may be revised.

The instruction reaches a one-shot review worker in its assignment, not in the coordinator's rules: `dispatch_prompt.compose` adds a `Review:` line, telling the worker to run `extras:code-review` and not fix, to a Tasks row whose `stage` cell is `review`. A `reviewer_session` gets its prompt from its own configuration and is not touched.

## 7. Assignments

An implementer's assignment tells the worker about the architecture: it names the canonical architecture document of the workspace the task belongs to, and the worker reads it before changing code. The work is held to that document.

The path is read from the `Canonical document:` bullet of the workspace's own `## Architecture` block, the first backticked path in it, resolved against the workspace root. `dispatch_prompt.compose` writes it as an `Architecture:` line in both the one-shot and the interactive assignment. A workspace without that block or bullet gets the assignment unchanged and no refusal.

## 8. Single responsibility

A module has one reason to change. `inbox.py` and `audit_tasks.py`, the two largest, and `flow.py` each carry more than one and are split by responsibility, each part landing in one circle of section 4.

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

Agent performance is measured from the one-shot launcher lines in the `## Log` of every day's tracker, listed by `workspace.daily_trackers`. No other store, cache or copy is kept. The one-shot reports are not a source, because a launch deletes the earlier reports for its task and a report holds no runtime, model or times. Interactive sessions are not measured.

The started line records the agent type as `type=<agent type>` after the effort, only when one is set, as it records the model and effort. `one_shot.record_launch` passes the type the launcher already receives, and every caller that launches a one-shot passes the launcher the type it dispatched, as `--type`, only when the workspace's `Agent:` lines declare that type. Without that declaration `--type` is left out, the default agent runs, and the started line records no type, as the dispatch rules require. The automatic dispatch in `flow.py` follows the same rule for the type it cut the worktree for, checked with `make_worktree.agent_types`. Runs started before this have no type.

A run is a started line paired with the same worker's completed, human-review or relaunch line, across consecutive days' trackers, so a run that ends after midnight keeps its end. The pairing is one function in `tracker_log.py`, which returns `Run` records, and every reader that pairs runs uses it: the issue page drops its own same-day pairing. A Log line's time is HH:MM on its tracker's day in the workspace zone, so a time in the hour repeated when daylight saving ends is read as its first occurrence; a run there can be off by an hour, and the Log format is not changed for it.

| Boundary | Signature | Circle |
|---|---|---|
| `tracker_log.Run` | `worker: str, task: str, runtime: str, model: str, effort: str, agent_type: str, started: datetime, ended: datetime \| None, outcome: str \| None` | Entities |
| `tracker_log.runs` | `(trackers: list[tuple[date, str]], zone: ZoneInfo) -> list[Run]` | Entities |
| `performance.Row` | `period: date, runtime: str, model: str, effort: str, agent_type: str, runs: int, completed: int, human_review: int, relaunched: int, median_minutes: int \| None` | Use Cases |
| `performance.table` | `(runs: list[Run], since: date, until: date, by: str) -> list[Row]`, where `by` is `day` or `week` | Use Cases |
| `performance_report.main` | `(argv: list[str] \| None = None) -> int` | Frameworks and Drivers |

For each day or week and each key of runtime, model, effort and agent type, the report counts the runs started, completed, sent to human review and relaunched, and the median whole minutes from start to end, taking the lower middle value for an even count. A run belongs to the period of its start, in the workspace zone. A week starts on Monday, and `Row.period` is the period's first day: the day itself, or the week's Monday. `--since` and `--until` are dates in the workspace zone and both are inclusive; `--until` defaults to today and `--by` to `day`. A run with no model, effort or type on its started line is keyed by what is there, and an empty key field prints as `-`. `performance.py` reads no file; `performance_report.py` reads the trackers, calls `performance.table` and prints one table. It is the `performance` entry of `COMMANDS` in `chief_of_stuff.py`: `chief-of-stuff performance --since DATE [--until DATE] [--by day|week]`. No board panel or page shows it.

Review findings are not counted until review reports have one shape (issue #563). Then findings per task are joined to runs by the task on the started line.

## 10. Runtime limits

Each runtime's limits are read without a billed call, recorded over time, and shown on the board: rolling windows, weekly caps and model caps, for claude, codex, cursor and agy. One reading covers every runtime. A limit that cannot be read is recorded as unknown with the reason, never as zero.

`limits.py` (Entities) holds the reading record and the pure judgements over readings: the burn rate across a series of readings, and whether a limit will reach its cap before its reset. It reads no file. `limits_probe.py` (Frameworks and Drivers) takes the reading, one function per runtime, only from sources that cost nothing: the runtime's local state or logs, or a status command that starts no model session. It never goes through `claude_adapter.py`, whose calls are billed and which serves the model port. An existing tool that already reads a runtime's usage is preferred to a new parser, and adopting one needs the owner's approval like any other dependency.

Limits belong to an account, not a workspace, so readings are account-wide: two workspaces on one account share one history. `limits_history.py` (Interface Adapters, gateways) keeps the readings behind the ORM, in their own SQLite database at `$XDG_STATE_HOME/chief-of-stuff/limits.sqlite` (default `~/.local/state/chief-of-stuff/limits.sqlite`), with migrations of their own. It reuses the store's migration setup through a shared helper rather than a copy. `store.py` stays the issue-keyed workspace store and holds no readings. Section 9's single source applies to agent performance and is not changed by this store.

`limits_cli.py` is the `limits` entry of `COMMANDS` in `chief_of_stuff.py`: `chief-of-stuff limits` takes one reading of every runtime, records it, and prints it. The board reads the history and judges it through `limits.py`. It never probes a runtime itself. The point-in-time query that chooses a dispatch runtime reads the same history.

| Boundary | Signature | Circle |
|---|---|---|
| `limits.Reading` | `runtime: str, limit: str, used: float \| None, cap: float \| None, unit: str, resets_at: datetime \| None, read_at: datetime, unknown: str` | Entities |
| `limits.burn_rate` | `(readings: list[Reading]) -> float \| None` | Entities |
| `limits.near_cap` | `(readings: list[Reading], now: datetime) -> bool` | Entities |
| `limits_probe.read` | `(runtime: str) -> list[Reading]` | Frameworks and Drivers |
| `limits_history.LimitsHistory.record` | `(readings: list[Reading]) -> None` | Interface Adapters, gateways |
| `limits_history.LimitsHistory.readings` | `(since: datetime) -> list[Reading]` | Interface Adapters, gateways |
| `limits_cli.main` | `(argv: list[str] \| None = None) -> int` | Frameworks and Drivers |
