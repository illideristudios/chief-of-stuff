# chief-of-stuff architecture

This is the canonical architecture document. The README is the user's guide and is not this document. Every section is numbered, numbers are never reused, and a review grades the code against these sections. The sections state the required architecture, not the present state of the code. Where the code does not yet meet a section, the gap is a row in `compliance.md`, in this directory.

The T3 integration design (`docs/specs/2026-10-29-t3-integration-design.md`, "the T3 design" below) amends this document the way the 1.0 ledger does. Its decisions are written here as decided, its author's calls are marked `call`, and its open items are listed, unsettled, in section 19. It is reconciled into sections 4, 6, 8, 9, 11, 12 and 13 and adds sections 15 to 19.

## 1. Style

The project follows Clean Architecture as Robert C. Martin defines it, and SOLID. The circles, from the centre outward, are Entities, Use Cases, Interface Adapters, and Frameworks and Drivers. The dependency rule: source code dependencies point inward only, so nothing in an inner circle names anything in an outer one. Single responsibility, open-closed, Liskov substitution, interface segregation and dependency inversion are the five principles a review holds each module to.

YAGNI and DRY limit both. An interface, factory or layer that no caller and no section of this document needs today is not added, and a copy of what already exists is not made. Where a section here and the code disagree, the section stands and the code is recorded as a gap in `compliance.md`.

## 2. Skills

A skill is loaded on demand on every host. The agent rules name the skill and the moment to read it, and the host reads the skill's file then. No host receives the text of a skill in its starting prompt.

## 3. Page modules

The shared helpers the page modules use (the stylesheet head, the file write that skips unchanged content, the day's context, the pending count, the duration span, and the markdown and section renderers) live in one module of their own and are public names. `decision_page.py`, `issue_page.py`, `source_page.py` and `render_board.py` import them from there. No page module imports another page module's underscore-prefixed name.

## 4. Circles

Each script under `scripts/`, the two launchers and each hook file belong to one circle. A module that does the work of two circles is a gap against this section until it is split. `inbox.py`, `audit_tasks.py` and `flow.py` are placed by their parts, as section 8 describes.

The owner decisions in the 1.0 ledger (`docs/specs/2026-10-09-chief-of-stuff-1.0-ledger.md`, section "Owner decisions", 2026-10-10) amend this document. Where a section here disagrees with one of them, the owner decision stands and the section is rewritten to match. A module the ledger retires, such as the file mailbox, the tracker parser and its command lines, the flow tick, the notify service, the terminal launchers or the non-Claude watcher, keeps its circle in this table until it is deleted. A gap against a retiring module closes when the module is deleted.

New work goes on the engine path: the store, the forge adapter, the model port, the reconcile pass and the pages built on them. The goal is 1.0, then 1.1, with as much legacy dropped as possible (owner, 2026-10-16), so getting the engine to run the workspace comes first. A retiring module takes only the fixes today's loop needs to keep running until that cutover. A new behaviour lands on the engine path. It also lands on a retiring module only when the loop needs it before the cutover, and then the engine side is in the same change or filed as its own issue. A change that adds behaviour only to a retiring module is a gap against this section.

| Circle | Modules |
|---|---|
| Entities | `tracker.py`, `md.py`, `clock.py`, `runtimes.py`, `policy.py`, `findings.py`, `orphans.py`, `estimate.py`, `tracker_log.py`, `tree_state.py`, the finding types of `audit_tasks.py`, the message format of `inbox.py`, `guard_commands.py` (the worker guard's pure shell-command judgements), `limits.py` (runtime limit readings and their judgements, section 10), `decision_graph.py` (the decision graph's leaves, ancestors and rejected alternatives, pure, importing no other script; section 17) |
| Use Cases | the judgements of `audit_tasks.py`, `dispatch_prompt.py`, `one_shot.py`, `kanban.py`, `merge_ready.py`, `merge_approved.py`, `review_threads.py`, `task_forge.py`, `model_port.py` (the model port), `issue_gate.py` (issue first, then one store row), `trio_gate.py` (the decision trio, design 5a), `decision_escalation.py` (trio retries, owner waits, reversals and replacement asks), `architect_signals.py` (section 16's judgements over gap rows, the pending filter, signal keys and issue bodies), `decision_history.py` (the harsh judgement of a history request from its text and the current leaves, and the precedent seam; section 17), `decision_actions.py` (a board decision's lifecycle: answer, reverse, re-route, and an answered decision whose action is blocked. The page server only validates and records a person's intent through it; the engine alone applies intents, see section 6), `engine_merge.py` (executes a planned merge, logged on the issue), `forge_port.py` (`ForgeError`, the forge failure boundary), `reconcile.py` (the engine's planner: what the state calls for), `readiness.py` (the Ready predicate over a pass's facts: dependency blockers and stop records), `completion.py` (judgements on a finished or abandoned run: done-on-main, the read-only rule, unlanded work and orphan recovery), `guard_rules.py` (the worker guard's Stop, permission-relay and session-start rules), the ready-task and stagnation judgements of `flow.py`, `performance.py`, `worker_health.py` (a worker's six health signals, the stall judgement and the per-issue failure count, sections 8 and 18), `heartbeat.py` (the heartbeat's status, its sink seam and the failure-tolerant delivery; section 18) |
| Interface Adapters, presenters | `fragment.py`, `columns.py`, `panels.py`, `gantt.py`, `flow_chart.py`, `render_board.py`, `decision_page.py`, `issue_page.py`, `source_page.py`, `module_graph.py`, `workers_page.py`, `page_reload.py`, `heartbeat_page.py` (the `/report` page, section 18), `decision_graph_page.py` (the owner's read-only `/decisions/graph` pages, section 17), and the page module of section 3 |
| Interface Adapters, gateways | `store.py` (the issue-keyed store and the workspace's engine log, such as policy changes, behind the ORM, and its `migrations/`, among them `0012_decision_graph` (section 17), `0013_transcripts` (section 9) and `0014_cleanup_teardown` (section 8)), `transcripts.py` (each worker attempt's transcript captured from its launch's own source into the store, section 9), `forge.py` (the one forge adapter for GitHub and GitLab), `backlog.py`, `backlog_ref.py`, `board_sources.py`, `session_logs.py` (each runtime's session log, found and read into one shape), `workspace.py`, `settings.py`, `tracker_read.py`, `tracker_write.py`, `forge_review.py`, `one_shot_report.py`, `tree_claims.py`, `slots.py` (the worker concurrency cap, E21), `limits_history.py` (the account-wide limit readings behind the ORM, section 10), `architect_signal_store.py` (reads `compliance.md` and keeps the coordinator's ledger file, never editing a row; section 16), `precedent_source.py` (the tracker and decision-graph precedent sources behind the seam; section 17), `heartbeat_store.py` (the heartbeat's facts read from the store, and its file sink; section 18), `private_state.py` (message-bearing files kept private), `github_admission.py`, `github_graphql_transport.py` and `github_scan.py` (host-wide admission of GitHub GraphQL requests, one admitted request per attempt, and the crash-resumable source scan), the git and forge fact gathering of `audit_tasks.py`, the mailbox storage of `inbox.py` |
| Frameworks and Drivers | `claude_adapter.py` and the other host adapters, the T3 launcher `t3_adapter.py` among them (section 13; host adapters are the only modules that spawn a host CLI, and the `t3` CLI counts as one), `git_trees.py`, `git_view.py`, the command line of `inbox.py`, the audit orchestration and command line of `audit_tasks.py`, `notify.py`, `notify_service.py`, `supervisor.py` (the engine venv and LaunchAgent), `self_update.py` (an installed engine on its own repository installs each new main of its upstream through the one install, section 14), `startup.py` (the engine and `work` commands' workspace settings and forge), `store_access.py` (the migrated store, under the migrate lock), `kanban_cli.py` (the `kanban` command line), `pipeline_cli.py` (the `pipeline` command line: a pipeline's status, or a branch's head and latest pipeline, read through `forge.Forge`), `process_status.py`, `session_exec.py`, `spawn_session.py`, `probe_health.py`, `pages.py`, `chief_of_stuff.py`, `start_coordinator.py`, `hooks/hooks.json`, `board_guard.py`, `init_workspace.py`, `install_model_guidance.py`, `make_worktree.py`, `migrate_backlog.py`, `one_shot_result.py`, `shell_setup.py` (the login shell, and the only place the workspace's `[workers] teardown` and `teardown_list` commands run; Use Cases receive teardown as a passed-in function, never by import), `cleanup.py` (engine-owned worktree removal behind every finding-27 guard, E19; it runs the workspace teardown in a tree before its container-mount guard), `engine.py` (the workspace singleton loop that executes the reconcile plan), `engine_facts.py` (gathers one pass's git, worktree, process and forge facts into plain records for the planner), `work.py` (the issue-first intake command line), `worker.py` (worker launch through the adapter and completion collection), `worker_guard.py` (the worker guard's hook entry point), `guard_binding.py` (the worker's binding read from its environment), `engine_mcp.py` (the engine's report and ask MCP server), `guard/hooks/hooks.json`, `adopt_one_shots.py`, `sync_page_theme.py`, the command line and tick orchestration of `flow.py`, `performance_report.py`, `limits_probe.py` (unbilled reads of each runtime's limits, section 10), `limits_cli.py` (the `limits` command line), `architect_signals_cli.py` (the `architect-signals` command line; section 16), `decisions_cli.py` (the `decisions` command line of the coordinator, the architect and the owner, which refuses a worker binding; section 17), `release_selection.py` (selects the installed release or a newer plugin-cache sibling), `engine_intent.py` (the engine's private ordered intent), `merge_order.py` (the `merge-order` command line, #75: it reads the open requests through `backlog.py`, trial-merges each overlapping pair in a scratch checkout through git, and prints the least-conflict order; its ordering judgement takes plain records and runs no I/O; see the note below the table), `bake.py` (the bake judge, M7.4 and #648: it judges a calendar week of the store against the 1.0 exit signs S1 to S4, reading engine starts from `engine-starts.jsonl`, which the engine appends through `supervisor.record_engine_start`; run as `scripts/bake.py`, it has no `chief-of-stuff` command) |

The T3 launcher is a host adapter (owner, 2026-10-29, T3 design §9). It implements the start, cancel and classify contract of section 13 and sits in Frameworks and Drivers beside the other adapters, so the rule that only host adapters spawn a host CLI holds without an exception: the `t3` CLI counts as a host CLI under that rule, and no module but the T3 launcher runs it. T3 is a launcher, not a fifth runtime (call, T3 design §9): `runtimes.py` keeps its four runtimes, and a worker launched through T3 is still a claude, codex, cursor or agy worker. The launcher is `t3_adapter.py` (#1178). It runs the `t3` CLI, writes the engine channel into the worker tree, and hands its host to `claude_adapter`'s detached runner, so the run evidence, cancel and collection are the contract's own. It imports `claude_adapter`, `host_adapter` and `shell_setup` (Frameworks and Drivers), `model_port` and `worker_health` (Use Cases) and `runtimes` (Entities), all in its own circle or inward, and, inside one function, the `mcp` client and `httpx2` packages already pinned in `requirements.txt` (section 13). It draws nothing.

```
Ad := the T3 launcher implements section 13's start, cancel and classify contract
Sp(m) := module m spawns a host CLI      Ha(m) := m is a host adapter      T := m runs the t3 CLI
Rt := T3 is a runtime in runtimes.py      In := every import of t3_adapter.py is in its circle or an inner one

1.  Ad                         [owner, 2026-10-29: T3 design §9, behind the same host-adapter contract]
2.  ∀m: Sp(m) → Ha(m)          [this section]
3.  ∀m: T(m) → Sp(m)           [architect: the t3 CLI counts as a host CLI]
4.  ¬Rt                        [call, T3 design §9: a launcher, not a fifth runtime]
5.  In                         [scripts/t3_adapter.py:28-33: claude_adapter, host_adapter, runtimes, model_port, shell_setup, worker_health]
∴   ∀m: T(m) → Ha(m), the rule needs no exception for T3, and t3_adapter.py in Frameworks and Drivers obeys section 1's dependency rule (5)
```

The architect signals of section 16 (#1180) are placed by their parts, one module each. The judgements over gap rows, the pending filter, signal keys and issue bodies are `architect_signals.py` (Use Cases, no I/O). Reading `compliance.md` and keeping the coordinator's ledger file are `architect_signal_store.py` (Interface Adapters, gateways). The `architect-signals` command line is `architect_signals_cli.py` (Frameworks and Drivers), which imports the other two. A module that holds two of the three parts is a gap against this section.

```
J := the signal judgements      G := the compliance read and the ledger file      C := the architect-signals command line
M(x, y) := x and y are in one module
1.  J ∈ Use Cases ∧ G ∈ gateways ∧ C ∈ Drivers      [section 1: the circles; this section]
2.  M(J, G) ∨ M(J, C) ∨ M(G, C) → gap                [this section: one module, one circle]
3.  ¬M(J, G) ∧ ¬M(J, C) ∧ ¬M(G, C)                   [architect_signals.py, architect_signal_store.py, architect_signals_cli.py; evals/test_architecture.py CIRCLES]
∴   the signal judgements stay pure, the file and command-line work sits in the outer circles, and no gap is open against this paragraph
```

The decision graph of section 17 (#1181) is placed the same way. `decision_graph.py` is an Entity: the graph's leaves, ancestors and rejected alternatives, importing no other script and no I/O. `decision_history.py` is a Use Case: the harsh judgement of a history request and the precedent seam, reaching no adapter and no I/O. `precedent_source.py` is a gateway: the tracker's Decisions rows and the graph's leaves behind that seam. The store keeps the graph (migration `0012_decision_graph`). `decisions_cli.py` is the `decisions` command line, in Frameworks and Drivers. The heartbeat of section 18 (#1179) is placed by its parts too: `heartbeat.py` (Use Cases) computes the status and delivers it, `heartbeat_store.py` (gateways) reads its facts from the store and writes the file sink, and `heartbeat_page.py` (presenters) renders `/report` from that file and never acts. `evals/test_architecture.py` holds each of these in the circle named here, and its dependency tests hold `decision_graph` to no imports and `decision_history` to no adapter and no I/O.

```
Gr := decision_graph.py      Dh := decision_history.py      Ps := precedent_source.py      Dc := decisions_cli.py
Hb := heartbeat.py      Hs := heartbeat_store.py      Hp := heartbeat_page.py      c(m) := m's circle
I(m) := every import of m is in c(m) or an inner circle
1.  c(Gr) = Entities ∧ c(Dh) = Use Cases ∧ c(Ps) = gateways ∧ c(Dc) = Drivers      [evals/test_architecture.py CIRCLES]
2.  c(Hb) = Use Cases ∧ c(Hs) = gateways ∧ c(Hp) = presenters                      [evals/test_architecture.py CIRCLES, PAGES]
3.  Gr imports no script; Dh imports decision_graph and md; Hb imports worker_health      [scripts/decision_graph.py, scripts/decision_history.py:14, scripts/heartbeat.py:16]
4.  Ps imports decision_graph, decision_history, store, workspace; Hs imports heartbeat, workspace; Hp imports heartbeat_store, decision_page, fragment      [scripts/precedent_source.py:13, scripts/heartbeat_store.py:12, scripts/heartbeat_page.py:9]
∴   I(m) holds for each of the seven (1 to 4), so section 1's dependency rule holds for this round's modules
```

The modules merged since are placed the same way, as `evals/test_architecture.py` holds them. `decision_graph_page.py` (pull request 1203) is a presenter: it renders the owner's view of the graph from a store snapshot handed to it and acts on nothing (section 17). `transcripts.py` (#1161) is a gateway: it reads one launch's own transcript file and writes the capture through the store, and migrations `0013_transcripts` and `0014_cleanup_teardown` are the store's, with it (sections 8 and 9). `self_update.py` (pull request 1157) and `bake.py` (#648) are in Frameworks and Drivers: the first runs git and the one install, the second is a command line over the store. `bake.py` also holds a pure judgement (`bake.judge`, `scripts/bake.py:305`) beside its store reading (`load_and_judge`, `:357`) and its command line (`main`, `:389`), which is the work of three circles in one module and is therefore a gap against this section (`compliance.md` row C45), as `inbox.py` and `audit_tasks.py` are until they are split.

```
Gp := decision_graph_page.py      Tx := transcripts.py      Su := self_update.py      Bk := bake.py      c(m) := m's circle
I(m) := every import of m is in c(m) or an inner circle      Two(m) := m holds the work of more than one circle
1.  c(Gp) = presenters ∧ c(Tx) = gateways ∧ c(Su) = Drivers ∧ c(Bk) = Drivers      [evals/test_architecture.py CIRCLES, PAGES]
2.  Gp imports backlog_ref, decision_graph, decision_page, fragment, task_forge      [scripts/decision_graph_page.py:16-20]
3.  Tx imports session_logs; Su imports chief_of_stuff      [scripts/transcripts.py:14; scripts/self_update.py:19]
4.  Bk imports decision_escalation, reconcile, supervisor, workspace, and in functions flow_chart, settings, store_access      [scripts/bake.py:14-17, :352, :358-359]
5.  Two(Bk)                       [scripts/bake.py:305, :357, :389]
6.  ∀m: Two(m) → gap              [this section]
∴   I(m) holds for all four (1 to 4), so section 1's dependency rule holds; Bk is a gap against this section until its judgement leaves the Drivers module (5, 6; C45)
```

The table lists `merge_order.py`, and the code has no such module: on main at a817cd13 there is no `scripts/merge_order.py`, and `evals/test_architecture.py` lists none. Pull request 1048 (#75, open) adds `scripts/merge_order.py` and a merge-order command. The row stands as written and nothing is decided here; it is reconciled when that request merges or is closed.

## 5. Enforcement

The dependency rule of section 1 is enforced by `evals/test_architecture.py`. It holds the circle table of section 4 and fails on any import from an inner circle to an outer one. Imports that violate the rule today are listed in that test by name, the list may only shrink, and each entry is also a gap in `compliance.md`.
## 6. Reviewers

Review is policy, as the 1.0 ledger settles (D7, F10, F12, F13; plan M4). Personas (role, model, effort, rules), rooms, stages, and the review policy are data: `assets/policy.toml`, read and validated by `policy.py` (Entities). Which personas review a stage, what counts as an open finding, the retry limits, and when the architect is consulted live there, with defaults per persona that a stage or room overrides.

The engine runs the review panel.
- `reconcile.py` (Use Cases) plans one review launch for each persona the policy names for the stage, and they run in parallel.
- It plans a fix for every open finding, so review burns down to none.
- It routes findings to the architect when the policy says to: the engine posts a request and waits, and the architect disposes of each finding as fix, file, keep or discard.
- After three fix rounds that end with findings still open, it parks the issue with those findings and a recommendation (section 15). The old ping after three rounds becomes this park (call, T3 design §5).
- `engine.py` executes the plan through the model port.

A review is bound to the head it reviewed: the segment records the reviewed commit, and a merge happens only when that commit is the change's current head.

The default is motion (owner, 2026-10-29, T3 design §5). The review panel, the fix loop and the merge gate stay, because they are automated checks and not stops. The merge gate merges with no wait on the owner once the review matches the change's current head and main's CI is green, and, where the policy requires the architect's approval, once the architect has given it for that head. The engine executes the merge (`engine_merge.py`); this is the T3 design's "the coordinator merges". A change surfaces for human review only on a contradiction (two current decisions, or a decision and the code), on a person's change request on the forge, or after three fix rounds with findings still open. Human review stays possible at any time.

```
M := the merge gate merges the change      Rv := the review matches the change's current head      Ci := main's CI is green
Rq := the policy requires the architect's approval      Ap := the architect approved the current head
X := a contradiction exists      Fx := three fix rounds ended with findings still open      Fc := a person requested changes on the forge
Hr := the change surfaces for human review      Pk := the issue is parked (section 15)      Wo := the merge waits on the owner

1.  Rv ∧ Ci ∧ (Rq → Ap) ∧ ¬X ∧ ¬Fx ∧ ¬Fc → M      [owner, 2026-10-29: "the default should be to try to make motion"]
2.  X ∨ Fx ∨ Fc → Hr                              [owner, 2026-10-29: "Human review surfaces when there are contradictions or issues or problems"]
3.  Fx → Pk                                  [call, T3 design §5: the three-round ping becomes a park]
4.  ¬Wo                                      [owner, 2026-10-29: merges waiting on the owner are removed]
∴   a merge waits on the architect only where the policy requires it, never on the owner, and the owner sees a change only when something is wrong
```

The gate is the same for every workflow. "The review" in the rule above is the engine's review panel, so a workflow with no review stage still has its change reviewed before it merges. A person's forge approval is human review and does not stand in for the panel. A person's forge change request is an issue raised on the change, so it holds the merge and the change surfaces for human review (owner, 2026-10-29: "Human review surfaces when there are contradictions or issues or problems"). That hold fires only on a problem and never by default, so it is not one of the waits section 15 removes. The fix-round limit is policy data like the other retry limits. Where the code disagrees, the rows are in `compliance.md`: a workflow with no review stage merges on the configured forge approver's approval alone (C37), code left from the removed hard stops is still threaded through the planner and the settings (C39), and the three fix rounds are a constant in the planner, not the policy's `retry_limit` (C40). The hold on a forge change request (`scripts/reconcile.py:296`) is this rule as built; C38 is withdrawn.

```
Rv := the engine's review panel reviewed the change's current head      Fa := a person approved on the forge      Fc := a person requested changes on the forge
M := the merge gate merges      Wo := the merge waits on the owner by default      Hr := the change surfaces for human review
Pb := a contradiction, an issue or a problem was raised
1.  M → Rv                      [rule 1 above: Rv is the review]
2.  ¬Wo                         [owner, 2026-10-29: the default is motion; merges waiting on the owner are removed]
3.  Pb → Hr ∧ ¬M                [owner, 2026-10-29: "Human review surfaces when there are contradictions or issues or problems"; T3 design §5]
4.  Fc → Pb                     [coordinator, relaying the owner's rule: a person's change request is such a problem]
5.  Fa ∧ ¬Rv → ¬M               [from 1]
6.  Fc → Hr ∧ ¬M                [from 3, 4]
∴   a forge approval never replaces the panel (5), and a forge change request holds the merge and surfaces for human review (6), which is a hold on a problem and not the default wait that 2 removes
```

The reviewer personas are the 22 personas of the `extras` plugin's code-review skill (1.2.0, `skills/code-review/personas/`). Each ships whole, as its own file under `assets/personas/`, copied with its source noted. A persona's rules are its full lens and numbered checks, never a one-line summary. Making the set pluggable is later work (owner, 2026-10-16).

The panel for a change follows the skill's own selection rule, which ships as policy data in `assets/policy.toml`:
- Every review gets Senior Architect (1) and Senior Staff Engineer (2).
- Personas are added by the change's types, from the skill's table: shell, library, UI, concurrency, database, distributed state, migration under writes, network or security, observability, security-sensitive, public API, tests, performance, state machines and invariants.
- Persona 11 is instantiated from the workspace's declared architecture style. For this repository that is "Uncle Bob", for Clean Architecture.
- Persona 22 is used only when the full panel is asked for.

Deciding which types a change has takes one model call through the port. The engine makes it and passes the types to the planner as a fact. Choosing the personas from the types and the table is pure, in `reconcile.py` and `policy.py`.

Each reviewer reviews only within its lens and does not fix. Findings come back in one review report shape (issue #563) and are kept as store records.

The architect and the architecture reviewers are different roles, and nothing treats one as the other.
- **The architect is the `frank-lloyd-aight` agent.** It owns the canonical architecture document and the compliance record. It is consulted when the policy says so, after the panel. It disposes of findings as fix, file, keep or discard, and gives or withholds the architecture approval a merge needs. It never runs as a panel reviewer, never sits in the decision trio, and reports no review findings of its own.
- **The architecture reviewers are panel personas:** Senior Architect (1) and the architectural-choice expert (11, "Uncle Bob" here). They review a change within their lens and report findings like any other reviewer. They cannot dispose of findings, approve a merge, or change the architecture documents.

In policy, the name `architect` is reserved for the agent: `[personas.architect]`, with `agent = "frank-lloyd-aight"`, is what the consult and dispositions point at. Reviewer personas never use that name; they are `senior-architect` and `uncle-bob`. A finding from an architecture reviewer goes to the architect for disposition like any other finding.

The engine never starts an architect run on its own (owner, 2026-10-17). The architect is a standing session, exactly one per project (owner, 2026-10-29, T3 design §4), that keeps its context across consults. Its three core responsibilities are keeping the documentation canonical, which on a new project means writing it first, planning, and drift checking. The canonical set is every file in the project's architecture directory, and nothing outside it. Subagents the architect spins off are its internal matter: they are not the coordinator's workers, not in the worker rotation, and not in the records of section 9 (call).
- **A consult is a request.** The engine posts it where the architect watches, on the change or its issue, in the form `Architect review requested: <sha>`, with the findings to dispose of. It then waits for the architect's reply on the same thread, and treats the issue as waiting on the architect until the reply comes.
- **The engine reads the reply as data:** `Architect: APPROVED <sha>` or `Architect: CHANGES <sha>`, plus one disposition per finding.
- **The head is the forge's.** "The current head" is the change's head as the forge adapter reports it, never a worker's local `HEAD`.
- **Only the approval binds to the head.** An approval counts for that head only, so a new head needs a new approval and one new request. As built (#1197), a reply counts only when its first line is exactly `Architect: APPROVED <sha>` or `Architect: CHANGES <sha>` for the full forge head, the engine posts one request per unapproved head, and at merge time it reads the thread again for the request's head and discards the planning pass's reading. A `CHANGES` reply sends the change down the workflow's fix edge.
- **Dispositions persist.** A finding disposed as fix, file, keep or discard stays disposed across heads, keyed by its finding. A request for a later head carries only the findings not yet disposed, and a `CHANGES` reply whose findings are all disposed needs no further request until the head moves. This is not built: dispositions are not stored, and each request carries every finding of the latest review (`compliance.md` row C36).
- **The architect grades and signals unprompted** (owner, 2026-10-29, T3 design §11). This replaces the rule that a new architect run happened only when the owner asked for one. The architect grades compliance after each merge to main and whenever a worker or the coordinator reports a mismatch between a decision and the code (call), records each gap as a row in `compliance.md`, and signals the coordinator as section 16 sets out. The policy, the planner and the trio never start a second architect session beside the standing one, and how the standing session is started and kept alive is the builder's.

```
Sa := the architect is one standing session per project      Mg := a change merged to main
Mm := a worker or the coordinator reported a mismatch      Gr := the architect grades compliance unprompted
Ow := an architect run waits for the owner to ask for it

1.  Sa                  [owner, 2026-10-29: T3 design §4]
2.  Mg ∨ Mm → Gr        [call, T3 design §6]
3.  ¬Ow                 [owner, 2026-10-29: T3 design §11 replaces the on-request rule]
∴   drift is graded when it can arise, with no prompt from the owner
```

Every question has one answerer (owner, 2026-10-29, T3 design §5). The owner and the architect decide architectural questions, under their own rules. The coordinator owns scheduling, priority and dispatch, and answers the questions in that scope. The decision trio answers the non-architectural decisions outside the coordinator's scope, and stays as this section describes it: three independent models, read-only, each question asked once. A trio that splits, or whose retry is exhausted, leaves the decision waiting on the owner, as the rules below already say (owner, 2026-10-29: "splits - require my input"). This is the one exception to section 15's rule that a non-architectural decision never waits. An agent that cannot tell whether a decision is architectural asks the architect.

```
D := the decision is architectural      Co := it is about scheduling, priority or dispatch      Q := the asker cannot tell whether D
Ar := the owner and the architect decide it      Ac := the coordinator answers it      Tr := the decision trio answers it      A := the asker asks the architect
Sp := the trio splits or exhausts its retry      Wo := the decision waits on the owner

1.  D → Ar              [owner, 2026-10-29]
2.  ¬D ∧ Co → Ac        [owner, 2026-10-29: "the coordinator owns scheduling and priority and dispatch"]
3.  ¬D ∧ ¬Co → Tr       [owner, 2026-10-29: "trio for decisions outside the coordinator's scope"]
4.  Q → A               [owner, 2026-10-29]
5.  Tr ∧ Sp → Wo        [owner, 2026-10-29: "splits - require my input"; T3 design §5]
∴   D, ¬D ∧ Co and ¬D ∧ ¬Co partition the questions, so each has exactly one answerer, and a split or exhausted trio hands its decision to the owner
```

The decision trio's models are not fixed (owner, 2026-10-18; issue #879). Its three personas keep their roles and judgement lenses from the policy, but each consult draws its models the way workers do.
- **A rotation class.** The workspace's `[models.trio]` rotation, with its `pick` (`first` or `random`), supplies the model for each trio member. A persona's own `model` is only the fallback when no rotation is set.
- **Three different models.** A consult draws three distinct launchable entries, preferring different runtimes or model families, so a unanimous verdict means three independent judgements, not one model asked three times. If fewer than three distinct entries can launch, it uses what it has and records that the trio was not fully diverse.
- **Every judgement is recorded.** Each member's vote names the runtime and model that cast it, so trio accuracy can be compared across models over time, as for workers (section 9).
- **Consults are read-only on every host.** A trio consult is a judgement call through the model port. It runs in each host's read-only or no-tools mode (Claude's `--safe-mode`, a read-only sandbox elsewhere), never with write access. A host that offers no read-only mode is not launchable for the trio.
- **Quota.** A quota stop moves that member to the next distinct entry. When no entry can run, the decision falls back to the owner as an exhausted retry.
- **Outage.** Any other model failure, such as a timeout or an answer with no structure, moves that member to another entry once (#928). A second failure is kept as the member's vote, and the decision is retried as an outage. A failure is never read as a concern. Each call has at most 300 seconds (owner, 2026-10-19: "we need to bump timeout").
- **Bounded per pass.** Consults run inside the engine pass, so all of a pass's consults share one budget of 480 seconds (#927), more than one call's limit so a slow vote leaves time for the others. No model call starts once the budget is spent, and each call's limit is cut to what remains of it. A consult that runs out of time is an outage, asked again at its retry time like any other. A permission ask is never denied for want of time.
- **Asked once.** A decision whose newest row waits on the owner, after a split or an exhausted retry, is not put to the trio again; later passes wait on it (#927). That wait on the owner stands (owner, 2026-10-29) and is section 15's one exception. A retryable outage is asked again only at its retry time.
- **Which denials bind.** A worker's permission ask is never an owner decision: the guard's rule or the trio answers it (#890). A deny binds every later attempt at the same request only when it is a trio verdict or a person's answer. A deny the engine records itself (the guard's rule, an unavailable trio, a trio that adopted no answer), an ask with no decision row, and a reversed decision stop only the stage and attempt that got them, so a retry asks again (#958). The rule is `guard_rules.denial_binds`, applied by `worker_guard.py`.

```
Mt := the trio's models come from [models.trio] (persona model only as fallback)
D := the three members use distinct entries, preferring different runtimes or families
U := a unanimous verdict reflects independent judgements      R := each vote records its runtime and model
O := every consult runs read-only on its host
F := a member's call failed for a reason other than quota      N := the member moves to another entry once
C := a failed call counts as a concern vote
T := a pass's consults fit within its 480-second budget        W := the pass waits on consults beyond that budget
Wo := a decision's newest row waits on the owner              K := the trio is consulted again on that decision
Dj := a permission deny was a trio verdict or a person's answer   Ba := the deny binds every later attempt at that request

Mt ∧ D → U          [owner 2026-10-18: "trio shouldn't be fixed models"]
R → trio accuracy is measurable per model (section 9)
O                   [a judgement call never writes]
F → N ∧ ¬C          [#928: an outage is not a concern]
T                   [#927: no call starts after the budget; each call's limit is cut to what remains]
T → ¬W
Wo → ¬K             [#927: the owner is asked each question once]
Ba ↔ Dj             [#958: guard_rules.denial_binds; an engine-recorded, missing or reversed deny binds only its own stage and attempt]
∴ the trio rotates like workers, stays diverse, stays read-only, survives one host's outage, never holds the pass, asks the owner each question once, and only a judged deny outlives its attempt
```

A person's actions on the board (answer, reverse, re-route) are intents, and the engine is their only executor.
- **The page server records, never acts.** It validates an intent through `decision_actions` and appends it to the store. It never writes to the forge, never moves a card, and never pops, blocks or supersedes another intent.
- **One current intent per issue.** Each pass, a pure function in `decision_actions` picks the newest pending intent for an issue and returns updates that mark every older pending intent superseded. A reverse is an intent like a re-route, so it supersedes too.
- **The engine applies it.** It checks that the intent is still current, and only then writes the stage to the forge and records the outcome. A failed write leaves the intent pending for the next pass. Only a permanent failure blocks the intent's own decision.
- **A pending intent holds its own issue only.** While it waits, the planner waits on that issue. It never marks sibling decisions blocked.
- **Decisions are append-only.** A decision's identity is its topic, question and instance, and the newest row for that identity is its state. Every answer, trio verdict, reversal, intent phase (write, run, done), supersession, block and unblock is a new row. No row is ever updated in place. `store.add_decision` is the only writer, reached through `trio_gate.record` for trio verdicts. A trio verdict never appends over an identity whose newest row is the owner's answer: the human answer wins. A reversal is an intent whose done phase is the `restarted` row the engine already writes.

```
I := an intent      C(I) := I is its issue's newest pending intent
W(I) := the engine writes I's stage to the forge      B(d) := decision d is blocked

the page server only appends intents   [no forge write in the driver]
W(I) → C(I)                     [currency is checked before any write]
¬C(I) → I is superseded         [decision_actions, pure]
B(d) only when d's own intent fails permanently   [never for siblings]
∴ one writer, one current intent per issue: a stale write cannot move a card, and no hold wedges another decision
```

Every decision has its own page at `/decisions/<id>`. The page server renders it from the store on request, with no authored file (owner, 2026-10-19: "there should be a per-decision page that is automatically rendered by id"; #945). `<id>` is the id of the first row of the decision's identity, so the address never changes as rows are appended, and the id of any later row of the same identity renders the same page. The page shows the identity's newest row as its state: the question, the options, the recommendation, the default, who it waits on, the trio's outcome, and the issue it belongs to, linked to that issue's page. Below that, every row of the identity is listed as its history. An open decision carries the answer form, which posts an intent like any other board action. An answered one shows the answer and who gave it, and no answer form. It keeps a Reverse action, which takes the person's reason ("why reverse it") and appends a reversal intent, so the recorded answer is never overwritten (#971; `scripts/decision_page.py:806`). An unknown id returns 404. The board shows no decision items and no answer form (owner, 2026-10-25: "rm inline decisions on the page, they're awful and I hate them"). Its NEEDS INPUT tile and Decisions tab link to the `/decisions` list (`scripts/render_board.py:321`, `scripts/fragment.py:85`), which is rendered from the store and links each decision to its page, as task pages do. `decision_page.py` is the only decision presenter, and a decision's page is the only place it is answered. The page's "Waits on" and the list's pending count read `decision_actions.waits_on_owner` (`scripts/decision_actions.py:105`), which follows `decision_escalation.needs_owner`, so a decision waits on the owner only when its newest row is the owner's unanswered wait or its intent is blocked, and a worker's permission ask never does (#1152). Until the legacy coordinator is deleted, its authored pages keep `/decisions/<slug>`, and a path made only of digits is a store id.

```
d := a decision identity in the store      P(d) := /decisions/<id of d's first row> renders d's page
S := the page is rendered from the store on request, with no authored file
F(d) := d's page carries a live answer form      A(d) := d's newest row is an answer
Bd := the board renders a decision item or an answer form      Rv(d) := d's page offers Reverse
Wo(d) := d's page says it waits on the owner      No(d) := needs_owner names d, or d's intent is blocked      Pa(d) := d is a worker's permission ask

∀d: P(d) ∧ S          [owner 2026-10-19; #945]
¬Bd                   [owner, 2026-10-25: "rm inline decisions on the page"]
A(d) → ¬F(d)          [an answered decision shows its answer and never a live form; #924]
A(d) → Rv(d)          [Reverse stays, takes a reason, appends; #971]
a form post is an intent → the page server records it and never acts   [this section]
Wo(d) ↔ No(d)         [#1152; scripts/decision_actions.py:105]
Pa(d) → ¬No(d)        [#890: a permission ask is never an owner decision]
∴ every decision has one stable address, it is answered there and never on the board, an answer is reversed there and never overwritten, both go through the one writer, and a permission ask never shows as waiting on the owner
```

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
| `audit_tasks.py` | The pure judgements | Use Cases |
| `audit_tasks.py` | Gathering facts from git and the forge | Interface Adapters, gateways |
| `audit_tasks.py` | The audit orchestration and the command line | Frameworks and Drivers |
| `flow.py` | The ready-task and stagnation judgements | Use Cases |
| `flow.py` | The tick orchestration and the command line | Frameworks and Drivers |

Each module keeps re-exporting the names its callers import until they import from the new modules. Where the module boundaries and their names fall is decided when the work is assigned. On the engine path, `flow.py`'s ready-task judgement lands in `readiness.py`, and its stagnation judgement lands in `worker_health.py`, beside the silence signal it shares a rule with. The audit's done-on-main, read-only, unlanded and orphan judgements land in `completion.py`.

On the engine path, the engine has no authorized files (Zach, 2026-10-23, issue #1039: file ownership broke the flow). This replaces the file-ownership rules of 2026-10-17.
- **No declared scope.** An issue names no paths, and nothing holds an issue for lacking them. Intake files an issue without a scope section, and the issue forms ask for none.
- **No scope decisions.** A plan proposes no scope, and the engine raises no `scope` decision and no file-ownership ask, so no issue waits on the trio or the owner for a scope.
- **No write gates.** No worker is refused a write for being outside a list, and no run is judged against a declared scope.
- **Overlap surfaces at merge.** Each worker runs in its own worktree, so two issues that edit one file meet as a merge conflict. The merge gate already requires a review of the current head, then main's CI, so the later branch rebases and is reviewed again on its new head. A conflict the rebase cannot settle goes back to the fix stage like any other finding, and a person is involved only when three fix rounds end with findings still open, which parks the issue (sections 6 and 15).
- **Worktree claims stay** only where they lock a tree for its own task. A claim never gates a write.

```
F := the engine has authorized files (declared scopes, scope decisions, ownership write gates)
Sd := a scope decision is raised      Hs := an issue is held for its scope      G := a write is refused for its path
C := two in-flight issues edit one file      M := C is settled at merge: rebase, re-review, and the fix stage on a conflict

1.  ¬F                  [Zach, 2026-10-23; #1039]
2.  Sd ∨ Hs ∨ G → F     [each exists only to carry F]
3.  C → M               [separate worktrees; merge gate: review of the current head, then main's CI]
∴   ¬Sd ∧ ¬Hs ∧ ¬G, and overlapping edits are settled at merge instead of before dispatch
```

On the engine path, abort and recovery never delete a worker's work. They quarantine it.
- **Abort quarantines.** When the engine abandons a run's tree, it moves the tree to a quarantine directory and renames the run's refs into a `refs/quarantine/<issue>/<stamp>/` namespace. The replacement is built fresh. Nothing is hashed, compared or deleted.
- **Quarantine is durable and visible to discovery.** Orphan discovery skips the quarantine namespace and directory by their names, which git itself stores, so an interruption cannot restore a set-aside candidate. A later launch never fast-forwards onto quarantined commits.
- **Recovery reuses only what is safe.** `completion.recovery` may reuse an orphan that is readable and not quarantined. Anything else is quarantined, and the run starts fresh. A failed fast-forward is quarantined too, not held.
- **A run whose host may still be running is never quarantined.** The engine first confirms the host has stopped. Until then the run waits.
- **Deleting quarantined work is a separate, explicit step**, not part of abort, and is 1.1 work. For 1.0, quarantine costs disk, not data.
- **Recovery is per issue.** Gathering one issue's recovery facts sits inside that issue's exception boundary, so one issue's failure never stops planning for the rest.

```
A := the engine abandons a run      Qn := its tree and refs are quarantined (moved and renamed, not deleted)
Del := work is deleted              L := the host has confirmed it stopped
R(o) := orphan o is reused          Rd(o) := o is readable      Qo(o) := o is quarantined

A ∧ L → Qn ∧ ¬Del        [abort never deletes]
¬L → wait                [never set aside a live run]
R(o) → Rd(o) ∧ ¬Qo(o)    [recovery reuses only safe, unquarantined work]
Del only by an explicit step   [1.1]
∴ no abort or recovery path can destroy work, and the abort machinery needs no content hashing, ignore rules or retire predicate
```

Removing a finished tree runs the workspace's teardown before the container-mount guard (#717, closed by #792; migration `0014_cleanup_teardown`). `cleanup.py` removes a tree only past every finding-27 guard, and the teardown is what stops a stack the tree started, so its mounts are judged after it.
- **Only on removal, once per tree.** Teardown runs only with `--remove`, never in a preview (`scripts/cleanup.py:241`). A tree whose other guards pass and whose teardown has not been attempted defers its mount guard (`:256-270`).
- **Under the launch lock, recorded before it runs.** Holding the lock workers register under, cleanup re-checks the guards, then claims the attempt in the store's `cleanup_teardown` table before running the command, so a timeout or an interrupted pass never retries it forever (`:296-300`). The attempt is keyed by the tree's admin directory and its backlink's identity and the command (`teardown_identity`, `:221`), so a tree recreated at the same path, or a changed command, gets one new attempt.
- **Every guard again after it.** The command runs through `shell_setup.teardown` (`:307`), and its warnings are reported and logged without blocking removal. The layout, claim and git guards run again, and a teardown that removed or replaced the worktree's metadata keeps the tree (`:308-322`). The container-mount guard runs after the teardown (`:323-324`).

```
Rm := a tree is removed      Gd := every finding-27 guard passes      Td := the teardown ran in the tree
Mg := the container-mount guard is judged      Cl := the attempt is claimed in the store first      Rr := the attempt is retried for the same tree and command
1.  Rm → Gd                   [scripts/cleanup.py:2-15]
2.  Td → Cl                   [scripts/cleanup.py:296-300]
3.  Cl → ¬Rr                  [scripts/cleanup.py:221, :258, :284]
4.  Td → Mg after Td, with every other guard re-run    [scripts/cleanup.py:308-324]
∴   a tree's stack is torn down at most once per tree and command, and its mounts are judged on the state the teardown left
```

On the engine path, a person's release of a human-review hold on an implement run accepts that run's work when its work product exists (issue #1030).
- **Release is observed, never assumed.** A release counts only when this pass's forge read shows the hold label gone. An omitted field is unknown, never a release.
- **The work product is the request review would open.** It is the open request `forge.closing_request` selects, the newest that closes only this issue, on the run's recorded branch. Its CI, approval and merge verdict do not matter here; the later gates still apply.
- **Present advances, absent reruns, unknown waits.** With the request present, the planner takes implement's success edge and every gate on it, as for a `done` run. With the request read and absent, implement runs again. When the hold, the branch or the request read is unknown, the issue waits with that reason.
- **The acceptance is derived, not stored.** The run's `human_review` outcome stays as recorded. A suite hold keeps its own acceptance path and is never accepted here. `engine_facts.py` gathers the evidence, and `reconcile.py` judges it.

```
H := this pass observed the hold cleared on the latest implement run, which ended human_review
W := the open request closing only this issue is on that run's recorded branch
Kw := W is known (the hold, the branch and the request read were all read)
A := the planner takes implement's success edge      Ri := implement runs again      Wt := the issue waits

1.  H ∧ Kw ∧ W → A        [#1030: the person's release accepts the pushed work]
2.  H ∧ Kw ∧ ¬W → Ri      [nothing to accept]
3.  ¬H ∨ ¬Kw → Wt         [missing evidence is never a release or an absence]
∴ a released implement run advances only to the request review would open, and never launches a duplicate attempt on a guess
```

## 9. Agent performance

Agent performance is measured from the store's stage segments, as the 1.0 ledger's owner decision A3 settles: a query over segments, not a parser of Log lines. A run is one segment, keyed (issue, stage, attempt), with its start and end times and its outcome. The engine records the segment when it launches a worker, through `store.record_segment` with the runtime, model, effort and agent type it launched with, and closes it with the outcome when it collects the worker. The tracker Log, the one-shot reports and interactive sessions are not sources.

The agent type follows the declaration rule: a segment records an agent type only when the workspace's `Agent:` lines declare the type the worker was launched as, checked with `make_worktree.agent_types`. Without that declaration the default agent runs and the segment records no type. Segments recorded before this have no runtime fields.

Segment times are full timestamps, so a run that ends after midnight keeps its end, and no daylight-saving rule is needed.

Each attempt's transcript is kept with its segment (#1161; `transcripts.py`, migration `0013_transcripts`). It is evidence of what the attempt did, not a performance source.
- **Bound to the launch, never discovered.** The store's `transcript` table holds one row per segment, unique by session and by source. The engine hands `transcripts.capture` the exact source of the launch it recorded (`claude_adapter.transcript_path`, `scripts/worker.py:741`), and never searches a tree or takes the newest session (`scripts/transcripts.py:3`). A run without a session id is not captured (`scripts/worker.py:737`).
- **Read on every collection pass, resumable.** `worker.collect` captures each recorded run on every pass (`scripts/worker.py:765`). Each capture reads only whole records past a stored offset and digest and commits them with the cursor in one store write (`Store.update_transcript`, `scripts/store.py:607`), so a restart resumes where it stopped. The source is opened without following a link (`scripts/transcripts.py:86`).
- **Capped, with the prefix kept.** The cap is `[workers] transcript_max_bytes`, 1 MiB by default (`scripts/settings.py:121`), pinned for an attempt at its first capture. Past it the prefix stays and every later event is dropped and counted (`scripts/transcripts.py:26-32`).
- **Gaps are marked, never filled.** An unavailable source, a malformed or non-object record, a partial last record of an ended run, and the cap each add a gap event with its reason. A source that was rotated, truncated or rewritten, or a record from another session, halts the capture with a gap, and the prefix already kept stays readable (`scripts/transcripts.py:78`, `:112`, `:150`).
- **Local Claude CLI runs only.** A T3 run and a non-Claude runtime are skipped: a T3 worker's source is its thread, never the provider's local files (`scripts/worker.py:734-735`).
- **Shown in memory, never published.** The workers page links each attempt to its session view, which the page server renders on request and never writes into the pages directory (`scripts/pages.py:983`).

```
Cp := an attempt's transcript is captured      Ls := the source is the one its launch recorded      Lc := the run is a local Claude CLI run
Ov := the kept events exceed the cap      Pk := the prefix is kept and later events are dropped and counted
Sc := the source was rotated, truncated or rewritten, or a record names another session      Ht := capture halts with a gap
1.  Cp → Ls ∧ Lc          [scripts/transcripts.py:3; scripts/worker.py:734-741]
2.  Ov → Pk               [scripts/transcripts.py:26-32]
3.  Sc → Ht               [scripts/transcripts.py:78, :112, :150]
∴   a kept transcript is one attempt's own events, bounded by the cap, and every hole in it is marked rather than filled
```

| Boundary | Signature | Circle |
|---|---|---|
| `store.Segment` | `issue_key: str, stage: str, attempt: int, started_at: str, ended_at: str \| None, outcome: str \| None, runtime: str, model: str, effort: str, agent_type: str, workflow: str` | Interface Adapters, gateways |
| `store.Store.rotation_choice` | `(issue_key: str, stage: str, attempt: int, task_class: str, eligible: tuple[str, ...], choose: Callable[[str], str], workflow: str = "") -> str` | Interface Adapters, gateways |
| `runtimes.following` | `(rotation: Sequence[T], after: str = "") -> tuple[T, ...]` | Entities |
| `performance.Row` | `period: date, runtime: str, model: str, effort: str, agent_type: str, runs: int, completed: int, human_review: int, relaunched: int, median_minutes: int \| None, workflow: str = ""` | Use Cases |
| `performance.Record` | `issue_key: str, stage: str, attempt: int, runtime: str, model: str, effort: str, agent_type: str, workflow: str, started_at: str, ended_at: str \| None, outcome: str \| None, completed: bool, human_review: bool, relaunched: bool, minutes: int \| None` | Use Cases |
| `performance.records` | `(segments: Iterable[Segment]) -> list[Record]` | Use Cases |
| `performance.table` | `(segments: Iterable[Segment], since: date, until: date, by: str, zone: ZoneInfo) -> list[Row]`, where `by` is `day` or `week` | Use Cases |
| `performance_report.main` | `(argv: list[str] \| None = None) -> int` | Frameworks and Drivers |

Each attempt's workflow is recorded with it (#1182). `Store.rotation_choice` reserves a rotation turn and the attempt's workflow in one transaction: the workspace store keeps one cursor per task class (the `rotation_cursor` table) and the attempt's choice in its resume record, so concurrent launches take separate turns and a recovered launch spends no extra turn. The selection policy stays with the caller, and `runtimes.following` gives one circular pass after the previous entry, so a retry or rework moves to the next entry. `record_segment` takes the workflow from that reservation when it is not passed. An older segment's unknown workflow stays empty and is never inferred from today's issue or policy. `performance.records` turns segments into raw attempt records, with the original outcome, both timestamps and the whole minutes, and computes no rate and no recommendation.

```
Rc := a turn and its workflow are reserved in one transaction      Cl := two concurrent launches take the same turn
Rv := a recovered launch reuses its reserved entry      Xt := recovery spends an extra turn
Wf := the attempt's workflow was recorded      If := a workflow is inferred for an older segment
1.  Rc → ¬Cl          [#1182: the store reserves each class's cursor and the launch choice atomically]
2.  Rv → ¬Xt          [#1182: rotation_choice returns a still-eligible prior choice]
3.  ¬Wf → ¬If         [#1182: an unknown workflow stays empty]
∴   the round robin is fair under concurrency and recovery, and the records hold only what was observed
```

For each day or week and each key of runtime, model, effort, agent type and workflow, the report counts the runs started, completed, sent to human review and relaunched, and the median whole minutes from start to end, taking the lower middle value for an even count. A run belongs to the period of its start, in the workspace zone. A week starts on Monday, and `Row.period` is the period's first day: the day itself, or the week's Monday. `--since` and `--until` are dates in the workspace zone and both are inclusive; `--until` defaults to today and `--by` to `day`. A segment with no model, effort or type is keyed by what is there, and an empty key field prints as `-`. `performance.py` reads no file. `performance_report.py` reads the segments through `store_access`, calls `performance.table` and prints one table. With `--raw` it prints the period's `performance.records` as JSON instead, one object per attempt. It is the `performance` entry of `COMMANDS` in `chief_of_stuff.py`: `chief-of-stuff performance --since DATE [--until DATE] [--by day|week] [--raw]`.

A page may show the same measures over time: each runtime, model and effort pair's trend and success rate per kind of task, with its sample size. The scoring is in `performance.py` (Use Cases), over segments passed in. Review rounds are the count of review-stage attempts per issue, the kind of task is the issue's workflow, and the pipeline result is read through the forge adapter, the source of truth. A presenter renders the page from rows handed to it and reads no file. Scoring makes no billed call and never blocks a launch.

Review findings are not counted until review reports have one shape (issue #563). Then findings are joined to runs by the issue key.

Workers fan out across the workspace's configured rotation entries in round robin, a retry goes to the next entry (section 15), and the engine records each entry's outcomes at each kind of work (owner, 2026-10-29, T3 design §8). The T3 design calls a rotation entry an "agent type": one configured runtime, model and effort. This document says rotation entry, because `agent_type` above is the declared type of the `Agent:` lines.
- **Records only.** No routing by success rate yet; scoring still never blocks a launch.
- **No new rotation.** The round robin and the next-agent retry extend the engine's existing rotation (section 13) and `[models.trio]` (section 6), and add none (call).
- **Near-cap entries are skipped** where limits are read (section 10). For a runtime whose limits are not read, the signal is a failed launch (call).
- **Raw outcomes are kept** (completed, sent to human review, relaunched, minutes), keyed as above by runtime, model, effort, agent type and the issue's workflow, so any later definition of success can be computed over them (call).
- **T3 runs** take their runtime and model fields from the thread's provider and model (section 13).
- **Every worker choice is configurable** (owner, 2026-10-29): strategy, tuning and launcher. Tuning is set by modes and policies. An auto mode lets the coordinator tune itself from the records. The definition of success is open, and auto mode is not built until it is settled. Other modes are named only once the system has been tried (section 19). The settings keys are the builder's.

```
Rr := workers fan out in round robin over rotation entries      Nx := a retry goes to the next entry
Nr := a new rotation is added      Rs := launches are routed by success rate      Sd := success is defined      Au := auto mode is built
Nc := the entry's account is near its cap and its limits are read      Sk := the rotation skips the entry

1.  Rr ∧ Nx           [owner, 2026-10-29]
2.  ¬Nr               [call: Rr and Nx extend section 13's rotation]
3.  ¬Rs               [owner, 2026-10-29: records only]
4.  Nc → Sk           [call, T3 design §8]
5.  Au → Sd           [owner, 2026-10-29]
6.  ¬Sd               [open, section 19]
∴   ¬Au (5, 6, modus tollens), and the records keep raw outcomes until Sd is settled
```

## 10. Runtime limits

Each runtime's limits are read without a billed call, recorded over time, and shown on the board: rolling windows, weekly caps and model caps. Today only claude's limits are read. They feed the quota-breach interrupt (plan M5, items 3 and 4). codex, cursor and agy workers now run (section 13, #857), but their limits are still recorded as unknown, with the reason "read in 1.1", until their probes land beside their adapters (#757). Until then, a quota stop on those hosts is caught only after it happens, by the host adapter's classification and the rotation's fallback (#851), never by a near-cap judgement in advance. A reading is one pass over every runtime the workspace can launch, each under the account it launches with: `limits_probe.read` is called once per runtime and account, and the results are recorded together. A limit that cannot be read is recorded as unknown with the reason, never as zero.

`limits.py` (Entities) holds the reading record and the pure judgements over readings: the burn rate across a series of readings, and whether a limit is near its cap before its reset. A limit is near its cap when its used share, or the share its burn rate projects at its reset, reaches `warn_at`: the `[limits] warn_at` setting, a fraction between 0 and 1 that defaults to 0.8 and is validated by `settings.py`. It reads no file. `limits_probe.py` (Frameworks and Drivers) takes the reading, one function per runtime, only from sources that cost nothing: the runtime's local state or logs, or a status command that starts no model session. A runtime's session log is read through `session_logs.py`, the gateway the Workers page also reads, and is never parsed a second time. It never goes through `claude_adapter.py`, whose calls are billed and which serves the model port. An existing tool that already reads a runtime's usage is preferred to a new parser, and adopting one needs the owner's approval like any other dependency.

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

What a workspace builds, deploys or runs, and how, reaches chief-of-stuff only through that workspace's own configuration: the `## Coordinator` block (its `Health:` lines and the rest; its human-only action list is removed, section 15) and the Settings TOML (such as `[workers] teardown` and `teardown_list`). chief-of-stuff runs those commands as given and never interprets them. A rule or a fixture that needs an example of a workspace-specific action uses an invented name, never a real product.

Two things are chief-of-stuff's subject, not a stack, and stay named: the host CLIs it coordinates (claude, codex, cursor and agy, in `runtimes.py`), and the forges it integrates with (GitHub and GitLab, behind `forge.py`). The repository's own CI tooling under `.github/` is how this project is tested, not something chief-of-stuff runs, and is outside this section.

## 12. Parent and child workspaces

One engine can work several workspaces at once (owner, 2026-10-17). A parent workspace lists child workspaces, and one engine runs them all. Issue #825.
- **Children are unchanged.** Each child is a full workspace: its own `CLAUDE.md` (Coordinator and Architecture blocks), backlog and forge, store, review policy and settings. Everything sections 4 to 11 say of a workspace holds for each child on its own.
- **The parent adds one table.** `[workspaces]` in the parent's settings maps a child's name to its path, relative to the parent root. A parent with its own Backlog is a workspace too, and is worked alongside its children. A child that cannot be read is reported and skipped, never fatal.
- **One engine, one pass per child.** `chief-of-stuff engine --root <parent>` runs the per-workspace pass once for each child in every loop. The pass is reused unchanged. A failure in one child is logged in that child's store and never stops another.
- **One slot pool.** The parent owns a slot pool that caps running workers across all children at its `[workers] max_concurrency`. Each child's own `max_concurrency` still caps that child. Runtime limits are already per account (section 10), so a near-cap judgement covers every child on that account.
- **One engine per workspace still holds.** The parent engine takes each child's singleton lock. A child already run by its own engine is skipped with a warning. Installing a child's own supervisor while a parent runs it is refused. The parent has one supervisor service.
- **One board index.** The parent board lists each child's board. Each child keeps its own board.
- **The meta coordinator** is an agent thread on the parent workspace (call, T3 design §4), which today has an engine pass and a slot pool but no agent. It reads the children's trackers and orders work across them. Each child's project coordinator prioritizes and executes within its own project. A cross-project dashboard is deferred (owner, 2026-10-29).
- **Not covered here:** dependencies between issues in different children. They wait for a need. The T3 design lists them as open (section 19), and `compliance.md` row C29 records the uncovered need.

```
Ch := a child is an unchanged full workspace      P := one engine runs the per-workspace pass for each child
G := one parent slot pool caps workers across children      A := limits are per account (§10)
X := a child's failure is contained to that child      O := each workspace has at most one engine
S := several workspaces are worked at once without overrunning the machine or the account

Ch ∧ P            [the existing pass is reused; owner 2026-10-17]
G ∧ A → no overrun of the machine or the account
X ∧ O             [per-child error boundary; child locks taken by the parent]
Ch ∧ P ∧ G ∧ A ∧ X ∧ O → S
∴ add [workspaces], the per-child loop and the parent slot pool; nothing in a child changes
```

```
Mc := the parent workspace has a meta coordinator agent thread      Pc := each child has its own project coordinator
Mc ∧ Pc           [call, T3 design §4: the meta and project coordinators map to the parent and its children]
∴ ordering work across children is the meta coordinator's, and nothing in a child's pass changes for it
```

## 13. Host adapters

Engine workers can run on Claude, Codex, Cursor and Antigravity (owner, 2026-10-17, pulling #700 and #701 forward). Each host is an adapter behind the model port, as design decision 7 intended.
- **One contract.** Every host adapter implements the same start, cancel and classify contract as `claude_adapter.py`, and writes the same run evidence in the run directory: `command.json`, the spawn record, `started.json` and `exit.json`. The engine, `worker.py` and recovery see no difference between hosts.
- **The launcher is configurable.** How a worker starts is set per workspace (owner, 2026-10-29, T3 design §9): a local CLI process, as today, or T3. Both are adapters behind this one contract, and the launcher is how a worker starts while the runtime is which agent it is. T3 launches claude, codex, cursor and agy workers with the provider and model of their rotation entry (call). Under T3, the T3 thread id is the worker's identity in the run evidence and the session registration, beside the engine's session id. Liveness and activity come from the thread's state (status, settled, item count, pending request count, update time), never from a PID, the process table or a transcript, and the board, the issue pages and the workers page link to the thread. The assignment text, the engine's report and ask channel, the store's segments and the forge post-checks are the same under either launcher. Every other bullet of this section holds for a T3 worker as for a CLI one; the next bullet says how.
- **A T3 launch is configured in the tree** (#1178; the spike notes of pull request 1188, run against T3 0.1.3). `t3 thread start` takes no environment, argv or MCP flags, and T3 cannot restrict a session to the user's own settings (`--setting-sources user --strict-mcp-config`).
  - **A tree with its own Claude config is refused.** A worker tree that already has a `.mcp.json` or a `.claude/` entry of any kind would run that config as this user, so `t3_adapter.py` refuses to launch it, fail closed.
  - **The channel and the guard are written in-tree.** The launcher writes `.mcp.json` with the engine's `report` and `ask` server and its `CHIEF_*` binding, and `.claude/settings.local.json` with the named agent and the guard plugin's hooks, each hook command carrying its own binding because a thread inherits no `CHIEF_*` environment. Both files are created exclusively (never over a file the engine did not write, never through a link) and git-excluded. The in-tree paths are named once, in the T3 launcher, because T3 is not a runtime and has no `runtimes.py` entry; for a host, its entry names them (next bullet).
  - **The in-tree hooks are a guard rail, not the boundary.** The worker can write them. A T3 run is therefore held to what a host without the guard plugin is held to: branch protection on the default and request-target branches before launch, and the post-run check on every protected branch after it. A T3 run adopted with no recorded branch heads is held.
  - **Launch parity holds without a T3 patch.** The `agent` setting in the tree's `.claude/settings.local.json` applies the named agent's prompt and tools, and the thread's `--model` wins over the agent file's `model:`, so the model comes from the rotation entry as on the CLI path. Threads run in T3's default `auto` mode, non-interactive, with no permission prompt for the project's server.
  - **Every worker tree becomes a T3 project.** The engine keeps cutting its own worktrees and registers each one as a T3 project (`t3 project add`), so the thread runs in that exact tree and T3 creates none. Removing such a project needs `--force`, which deletes its threads, so the launcher leaves them registered and the board's links keep working. The cost is a T3 project per worker tree.

```
Lt := the worker is launched through T3      Cf := the tree has its own .mcp.json or .claude/      Ln := the launch proceeds
Ch := the engine's channel and the guard hooks are written in-tree, exclusively and git-excluded
Pb := branch protection and the post-run check apply      Pj := the tree is registered as a T3 project      Rm := the project is removed      Th := its threads are deleted

1.  Lt ∧ Cf → ¬Ln          [spike 2, #1178: T3 cannot restrict a session to the user's settings]
2.  Lt ∧ Ln → Ch           [spike 2: a thread takes no environment, argv or MCP flags]
3.  Lt → Pb                [the in-tree hooks are writable by the worker, so they are a rail; this section's check is the boundary]
4.  Lt ∧ Ln → Pj           [spike 3: thread start takes only a registered project]
5.  Rm → Th                [spike 3: project removal needs --force, which deletes its threads]
∴   a T3 worker runs only in a tree with no config of its own, carries the same channel and checks as any worker, and its tree's project is kept so its thread stays linked
```
- **The registry builds the command.** Each host's argv, prompt bootstrap and environment come from its `runtimes.py` entry (Entities). No adapter imports the one-shot launcher, which retires (#830). Process plumbing shared by the adapters lives in one place (DRY). Each host's argv and error classification stay in its own entry or adapter (SOLID).
- **Outcomes reach the engine through the report tool.** The engine's `report` and `ask` MCP server is registered for every host at launch, with the same binding, and its configuration lives outside the worker tree, except on a host that reads it only from the tree (Zach, 2026-10-25). Cursor is that host: the 2026-10-17 spike found it reads only `.cursor/mcp.json` in the tree. The in-tree path is named in that host's `runtimes.py` entry. The engine writes the file at launch and keeps it out of git through the tree's `info/exclude`. It never replaces a file there that it did not write, and cleanup treats a `.cursor/` holding only that file as clean. It is callable without an approval prompt on every host. A host mode that drops plugin servers (Claude's `--strict-mcp-config`) is given it explicitly, and a host that refuses calls needing approval (Codex's `-a never`) approves the engine's server and no other (#952). A worker that stops without a report is "stopped without reporting an outcome", whatever the host.
- **The guard holds on every host.** The Claude plugin's PreToolUse gate does not load on other hosts, so a non-Claude worker gets a host-neutral guard with the same judgements:
  - **Shims:** a `PATH` shim directory, placed first by the engine, wraps `git`, `gh` and `glab` and refuses what `guard_commands` refuses: merges, pushes to protected or ambiguous refs, and forge calls outside the allowlist. It reuses `guard_commands` rather than copying it. A shim, like the hook, is a guard rail. The forge and the engine are the boundary.
  - **Branch protection:** a non-Claude entry launches only in a workspace whose forge protects its default branch and the branch its requests target, which can differ, for example `main` and `develop`. Otherwise the worker moves to the next entry in its rotation. This holds under either launcher. It is a launch precondition, not a human gate, so the removal of the hard stops (section 15) leaves it in place (call, T3 design §11).
  - **The engine checks after the run, on every protected branch.** It records each protected branch's head and the run's start time when a non-Claude run starts. At collect, every first-parent commit a protected branch gained during the run must be either a merge the engine recorded, as structured data (request and merge commit), or the merge commit of a request the forge records as opened before the run started (Zach, 2026-10-24, #949). The second case is read from the forge's own record of the request behind that commit (its number, its merge commit, its creation time), which a worker cannot write. It is never inferred from log text, a commit subject, a branch name or a time window. Any other commit, whether a worker's merge or a direct push, holds the run for human review, naming each commit and its branch. An unreadable branch, or a forge record that cannot be read, holds too. The known residue: a worker that pushes onto an older request's branch and gets it merged outside the engine passes this check, so the shims and branch protection stay the first line.
- **Quota and outage** are classified per host into the model port's retryable kinds, never by guessing at success.

```
H := a host adapter implements the model port's contract and run evidence
R := argv and environment come from runtimes.py      G := the shim reuses guard_commands' judgements
B := the forge protects the default branch and the request target branch
Rm(c) := commit c is a merge the engine recorded      P := every protected branch
Fr(c) := the forge records c as the merge commit of a request opened before the run started
N := a non-Claude worker may launch      Ok := the run passes the post-run check
Ct := the engine's MCP config is in the worker tree      Hc := the host reads its MCP config only from the tree
Nr := the in-tree path is named in the host's runtimes.py entry      X := the file is git-excluded and replaces no file the engine did not write

H ∧ R            [design decision 7; #830 retires the one-shot argv]
Ct → ((Hc ∧ Nr) ∨ Lt3) ∧ X [the owner, 2026-10-25: option A; Hc holds for Cursor alone among hosts, 2026-10-17 spike]
Lt3 := the worker is launched through T3, whose launcher names the in-tree paths   [#1178: T3 is no runtime, so it has no runtimes.py entry]
N → G ∧ B        [the guard holds on every host; an unprotected branch means the next entry]
Lt := the worker is launched through T3      Ln := the worker is launched as a local CLI process
(Lt ∨ Ln) ∧ N → G ∧ B      [owner, 2026-10-29: both launchers sit behind this contract; call, T3 design §11: B is a launch precondition and stays]
Ok ↔ ∀b ∈ P, ∀c ∈ first-parent(start(b)..end(b)): Rm(c) ∨ Fr(c)      [unreadable branch or forge record → ¬Ok; #860, #949 (Zach, 2026-10-24)]
Fr(c) is read from the forge's record, never from c's subject or a branch name      [a worker writes those]
¬Ok → held for human review, each commit named, never silent
∴ other hosts run behind the same port, the same evidence and an equivalent guard, and only a host that must read its channel from the tree gets one there
```

## 14. Release installation

One install puts one release everywhere chief-of-stuff runs. `chief-of-stuff install` copies the release and moves `current`, and the same install brings the Claude Code plugin to that release (owner, 2026-10-21, on finding the plugin at 0.129.69 while the engines ran 0.129.113: "that's not functionally complete").
- **The plugin installs from the installed release.** The `chief-of-stuff` marketplace's source is the `current` release pointer, never a git worktree, so the plugin can only be at a release that was installed.
- **The install updates the plugin.** After `current` moves, the install refreshes the marketplace and updates the plugin in every scope it is installed in. The `claude plugin` commands run through `claude_adapter.py`, because host adapters are the only modules that spawn a host CLI (section 4).
- **A plugin left behind is reported, never silent.** When Claude Code is missing or the update fails, the engines keep the new release, and the install reports the plugin's version beside the release's. It never reports the release as installed everywhere. A running Claude session loads the new plugin when it restarts, and the install says so.

```
I := an install of release r completes        Pc := `current` points at r
M := the marketplace source is `current`      U := the install refreshes the marketplace and updates every plugin scope, through claude_adapter
Pl := every installed plugin scope is at r    F := the install reports the plugin's version beside r

I → Pc            [chief_of_stuff.install]
M ∧ Pc ∧ U → Pl   [this section]
¬Pl → F           [this section; owner, 2026-10-21]
∴ I → Pl ∨ F: no install leaves the plugin silently behind the engines
```

## 15. Autonomy

The owner hands a project over, walks away, and returns to significant progress with no surprise stalls (owner, 2026-10-29, T3 design §1). The default is to proceed.
- **The hard stops are removed entirely** (owner, 2026-10-29). These are the coordinator's human-only action list, the lane gates and the yes-gates between pipeline stages, merges waiting on the owner (including the legacy `merge_owner` modes `approval` and `user`), and every guard hook and command rule whose job is to enforce one of them. Commit, push, merge and deploy proceed unattended unless the decision is architectural.
- **What stays is automation, not a stop.** The review panel, the fix loop and the merge gate of section 6 stay. The worker guard of section 13 and the Claude plugin's worker hooks stay: they keep merges, protected pushes and stage writes with the engine's merge gate and wait on no person, so they enforce none of the removed stops (architect, reading T3 design §5). Branch protection for non-Claude entries stays as a launch precondition (section 13). Rules set in a user's or host's own settings, outside chief-of-stuff, are unaffected (T3 design §3).
- **Waiting for an architectural decision is not a hard stop** and stays. Such a decision belongs to the owner and the architect (section 6).
- **A non-architectural decision never waits, with one exception:** a decision trio that splits, or whose retry is exhausted, leaves its decision waiting on the owner (owner, 2026-10-29: "splits - require my input"; section 6). Every other non-architectural decision proceeds without the owner or the architect.

```
D := the decision is architectural      U := the work waits for the owner or the architect      H := one of the old hard stops applies
Wg := the worker guard and branch protection apply      P := a person is waited on
Tr := the decision trio answers it      Sp := the trio splits or exhausts its retry      Wo := the decision waits on the owner

1.  D → U                   [owner, 2026-10-29]
2.  ¬D ∧ ¬(Tr ∧ Sp) → ¬U    [owner, 2026-10-29: the default is autonomy]
3.  Tr ∧ Sp → Wo            [owner, 2026-10-29: "splits - require my input"; T3 design §5]
4.  ¬H                      [owner, 2026-10-29: kill the existing hard stops entirely]
5.  Wg → ¬P                 [section 13: the guard confines merges to the engine; branch protection is a launch precondition]
6.  H → P                   [a hard stop is a wait on a person]
7.  ¬P → ¬H                 [6, contrapositive]
∴   commit, push, merge and deploy proceed unattended unless D or a split trio (2, 3), and removing H leaves Wg standing (5, 7)
```

Every piece of unattended work ends in one of three outcomes (owner, 2026-10-29, T3 design §5).
- **Do:** no decision is needed. Finish it.
- **Do on a default:** a decision is needed, a sensible default exists, and undoing it is cheap. Take the default on separable work and log the assumption.
- **Park:** the decision is architectural, or the work failed twice. Park it with its preparation done (research, options and a recommendation), so the owner's decision is one answer. Two failures park an issue even when a default exists (call).
- **Failed twice** is counted per issue, across agents, and a relaunch after a stall counts as a failure (call).
- **A retry goes to the next agent** in the rotation (owner, 2026-10-29). The failure counts against the first agent's record and the success against the second's (section 9).

```
Pk := the item is parked      Df := proceed on a default      Do := just do it
D := the decision is architectural      F := the work failed twice, per issue, across agents, a stall relaunch included
E := a decision is needed, a sensible default exists, and undoing it is cheap

1.  D ∨ F → Pk          [owner, 2026-10-29: parking limited to architectural or two failures]
2.  E ∧ ¬D ∧ ¬F → Df    [owner, 2026-10-29: do on a default; call: F parks even when E]
3.  ¬(D ∨ F ∨ E) → Do   [owner, 2026-10-29]
∴   a stall is justified only by D or F; every other stall is a bug
```

## 16. Roles and handoffs

Every role is a T3 thread the owner can open, read and message (owner, 2026-10-29, T3 design §4). The standing roles are the meta coordinator (section 12), one project coordinator per project, and one architect per project (section 6). The short-lived roles are the issue-filing worker, the implementation worker and the reviewer and other workers of today. The owner talks to the coordinator about coordination and to the architect about architecture, and can step into any worker. Handoffs between the standing roles are automatic; the owner sometimes directs one by hand.

The architect plans; the coordinator prioritizes and executes. Whether a fix changes the code or the architecture is the architect's call, and the coordinator only schedules the work. The architect never files issues (owner, 2026-10-29). Both handoffs below use the same short-lived issue-filing worker, which files forge issues and ends.
- **Drift.** The architect detects drift and writes a drift document. The coordinator spawns an issue-filing worker, which files the issue and ends. The issue joins the coordinator's queue, and a later worker picks it up under the coordinator's rules.
- **New architecture.** The architect marks the architecture, or a section of it, settled, at a granularity of its choosing. The coordinator spawns an issue-filing worker for what was marked.
- **The channel is new.** No channel from the architect to the coordinator exists today, and its mechanism (files, thread messages, or both) is the builder's. The intake is the architect's `unassigned` gap rows in `compliance.md` (call).
- **Only the architect writes `compliance.md`.** The coordinator keeps its own ledger of the row ids it has turned into issues and never edits the rows (call).
- **A signal files nothing twice** (call). Each settled marker and drift document names the commit it was written against, and the issue-filing worker checks the forge for an existing issue with the same key before it files.

```
Dr := the architect records drift      Se := the architect marks documents settled
Fw := an issue-filing worker is spawned, files, and ends      Iq := the issue is in the coordinator's queue      Wk := a later worker implements it
K := an issue with the signal's key already exists on the forge      Fi := the filing worker files a new issue

1.  Dr → Fw ∧ Iq     [owner, 2026-10-29]
2.  Se → Fw          [owner, 2026-10-29: granularity is the architect's]
3.  Iq → Wk          [owner, 2026-10-29: under the coordinator's rules]
4.  K → ¬Fi          [call, T3 design §6]
∴   no human step lies between recorded drift or a settled architecture and a fix, and a repeated signal files nothing twice
```

## 17. Decision graph

The coordinator answers worker questions in its scope (section 6) from precedent (owner, 2026-10-29, T3 design §7). Today precedent is the Decisions rows of the tracker. The answer source is a seam: the decision graph replaces those rows behind it without a change to the coordinator. The graph's storage and schema, and its tool's interface, are the builder's, within the rules below. A justified request reaches the coordinator, and its verdict reaches the blocked worker, without the worker polling in a model loop.

The graph exists to prevent decision poisoning: superseded decisions left in a context window and mistaken for current ones. Its rules, all decided (owner, 2026-10-29):
- **Leaves only, by default.** An agent sees the current decisions, those nothing has replaced.
- **A leaf is a whole decision.** It carries the full decision text as a usable unit, not a diff, with its reason. The reason of a current leaf is always granted.
- **Provenance is by links to parents.** A leaf may replace several earlier decisions, so the structure is a graph, not a tree. Rejected alternatives live only in ancestors, never in a leaf.
- **One tool.** History is reached only through the approved tool, the only way an agent reaches the graph. The coordinator, the architect and the owner read freely, and should not routinely, to protect their context. The owner traverses the graph on its own pages (below).
- **History needs a justified, approved request.** The tool refuses a request for ancestors and tells the worker to justify it. The justification goes to the coordinator, which is harsh and judges the text without reading the history. On approval, the tool returns the slice straight to the worker, as far up as the justification requires and the coordinator approves. Every grant is logged (call).
- **Grant:** two current decisions contradict, and the request states exactly what each requires that cannot both hold; or a current decision does not match what the code shows, and the mismatch is also reported to the architect.
- **Deny:** a reviewer finding that contradicts a leaf, on which the coordinator rules from the leaf and its reason; "more context"; "looks related"; "the decision is unclear"; "double-checking my own work"; any request that meets no grant case.
- **Was this approach rejected?** A worker that names the specific approach it is about to build, and why the answer is not obvious, may ask. On approval the tool answers only that question: the matching rejected alternative and its reason, and no ancestors (owner, 2026-10-29: "grant it as a narrow answer"). A vague request, such as "has anything like this been tried", names no approach and is denied (call).

```
Rq := a worker requests history      J := the request states a justification      Ap := the coordinator approves
G := the justification meets a grant case      Rd := the worker receives the ancestors, as far up as approved
Fr := the requester is the coordinator, the architect or the owner      Lg := the grant is logged
Rj := a worker asks whether approach a was already rejected      Nm := the request names a and why the answer is not obvious
Hit := a rejected alternative matches a      Sl := the tool returns that alternative and its reason, and no ancestors

1.  Rq ∧ ¬J → refused, told to justify      [owner, 2026-10-29]
2.  ¬G → ¬Ap                                [owner, 2026-10-29: the coordinator is harsh]
3.  Rq ∧ J ∧ Ap → Rd                        [owner, 2026-10-29]
4.  Fr → reads freely                        [owner, 2026-10-29]
5.  Ap → Lg                                  [call, T3 design §7]
6.  Rj ∧ ¬Nm → ¬Ap                           [owner: harsh; call: a vague request names no approach]
7.  Rj ∧ Nm ∧ Ap ∧ Hit → Sl                  [owner, 2026-10-29]
∴   history reaches a worker only on a justified, approved and logged request, and a rejected approach reaches it without the history around it
```

As built (#1181), the rules above hold this way:
- **The tool.** A worker reaches the graph only through the engine MCP server's `decisions` tool: the current leaves, or a request for ancestors or for whether an approach was rejected. The coordinator, the architect and the owner use the `decisions` command line (`decisions_cli.py`), which refuses to run under a worker's binding.
- **A grant is capped to the depth the justification names.** The coordinator's judgement (`decision_history.judge_justification`) grants one level unless the justification itself names more (a number of levels, "grandparent", "two hops"), and never more than the worker asked for. Every grant is a row in the store's grant log. Where the coordinator records a verdict by hand (`decisions decide`), the depth is not yet capped (`compliance.md` row C41).
- **No direct read.** The worker guard denies a worker's `sqlite3` (or `sqlite`), any run of `decisions_cli.py`, and `chief-of-stuff decisions`, with the reason that history is reached only through the tool. The guard is a Claude Code hook, so a worker on a host without that Bash guard can still open the store directly (`compliance.md` row C42).
- **The worker waits without polling a model.** The tool records the request, opens a named pipe for its verdict, and only then posts the request to the coordinator. Each wait cycle checks the store first and returns at once when a verdict is already there; otherwise it waits on the pipe, registered as a reader on the server's event loop, which the coordinator's verdict writes to. A one-second recheck of the store covers a pipe that could not be opened or a missed wake.
- **The graph's storage is reversible.** Migration `0012_decision_graph` keeps the graph in the store and has a downgrade, as does `0011_rotation_records` (#1187). Migrations `0001` to `0010` have none, so the store steps back no further than `0010`.
- **The owner's view is two read-only pages** (pull request 1203, `decision_graph_page.py`). `/decisions/graph` lists the current leaves, each with its full decision text and reason and a link to `/decisions/graph/<id>`. That page shows one decision, its parents and its children as links, and, only for a superseded decision, its rejected alternatives with their reasons (`scripts/decision_graph_page.py:167`), so a leaf never shows one. Both render on request from a snapshot of the store's graph (`Store.decision_graph_nodes`, `scripts/store.py:1089`). They call no history tool, log no grant and write nothing to the store, and the page server refuses any POST to them (`scripts/pages.py:1216`). They are cycle-safe: the parent and child maps are deduplicated and built in one pass over the nodes and edges, so a cycle in the stored edges cannot loop the render (`_graph_maps`, `scripts/decision_graph_page.py:44`). A path under `/decisions/graph/` that is not a number is refused with 404 (`scripts/pages.py:699-703`), and an id the graph does not hold renders no page and removes any stale file for it (`scripts/decision_graph_page.py:239-241`). The decision pages link to the view (`scripts/decision_page.py:1040`).
- **The pages are open to any local reader.** The page server serves them to every request, and the rendered files sit in the pages directory, so a worker that fetches `/decisions/graph/<id>` or reads `decisions-graph-<id>.html` reaches ancestors and rejected alternatives without the tool, and the guard denies neither (`compliance.md` row C46).

```
Rq := a worker requests history      Tl := the request goes through the decisions MCP tool      Gd := the guard denies the command
Dr := the command reads the store or the decisions CLI directly      Cg := the worker's host runs the Claude Code Bash guard
Dj := the depth the justification names      Rd := the depth the worker asked for      Ad := the approved depth
Vr := a verdict is recorded      Pw := the worker's tool is waiting on the pipe      Wk := the worker's tool returns
1.  Dr ∧ Cg → Gd                         [scripts/guard_commands.py:72, :428; guard/hooks/hooks.json:4]
2.  Dr ∧ ¬Cg → ¬Gd                       [guard/hooks/hooks.json:4: the guard is a Claude Code hook; C42]
3.  Ad = min(Rd, Dj), Dj = 1 when the text names no depth      [scripts/decision_history.py:180; owner, 2026-10-29: "as far up as the justification requires"]
4.  Vr → Wk                              [scripts/engine_mcp.py:132: the store is checked first; :149: the pipe wakes the loop]
∴   on a Claude host every worker read of history goes through the tool (1), the depth granted never exceeds what the justification names (3), and a verdict reaches the waiting worker without a model loop (4); on other hosts, 2 is the open gap
```

```
Ov := the owner's graph pages render      Lg := a grant is logged      Wr := the render writes to the store or calls the history tool
Cy := the stored edges contain a cycle      Hl := the render terminates      Pg := a worker fetches or reads the graph pages      Tl := the read goes through the tool
1.  Ov → ¬Lg ∧ ¬Wr                     [scripts/decision_graph_page.py:1-6, :210-243; scripts/pages.py:1216]
2.  Cy → Hl                            [scripts/decision_graph_page.py:44: deduplicated maps built in O(V + E)]
3.  the owner reads freely             [owner, 2026-10-29: rule 4 above]
4.  Pg ∧ ¬Tl                           [scripts/pages.py:697-703 serve every GET; no guard rule names the page server; C46]
∴   the owner's view is read-only and cannot loop (1 to 3), and the open item of section 19 is closed; a worker's read of those pages bypasses the tool (4), which is gap C46
```

## 18. Observability, heartbeat and stalls

The coordinator's dashboards are shown in T3 (owner, 2026-10-29, T3 design §10). The board, the decisions, the per-issue pages and the flow and Gantt charts are still rendered by the page server from the tracker and the store, and T3 shows them. Each task links to its worker's thread. The page server still records and never acts (section 6).

Chief-of-stuff drives T3 from outside and does not fork it (T3 design §3). A patch to T3 is a last resort (owner, 2026-10-29): it is written only after a live test shows that a needed capability cannot be had from outside, it is as small as possible, and it is kept as a patch file outside the T3 tree.

```
Nd := a capability the design needs      Ob := it can be had from outside T3      Lt := a live test showed that it cannot      Pt := T3 is patched
1.  Pt → Nd ∧ ¬Ob ∧ Lt      [owner, 2026-10-29: "last resort"]
2.  ¬Lt → ¬Pt               [from 1]
∴   no patch is written on a guess
```

The engine has a heartbeat, in both of two forms (owner, 2026-10-29).
- **A. A status thread.** A standing T3 thread whose title carries the engine's status, such as alive time, running count and waiting count. It costs no model turns. It needs the rename path from outside T3, which is untested (section 19).
- **B. The pinned board.** The board pinned in a T3 Browser panel, with the heartbeat's age at its top. T3 does not keep a pin across restarts. If the rename path fails, B alone carries the heartbeat (call).
- **Deep links.** A dashboard page links to a thread as `t3code://threads/<environment>/<thread>`. The `<environment>` part is read from T3's data directory (its `environment-id`) and is never hard-coded (call). The `t3-thread://` form works only as a chat chip and is never written into HTML. Behaviour in the desktop app is untested (section 19).

The heartbeat's status is computed once and delivered to sinks (#1179).
- **One status, two forms.** `heartbeat.status` (Use Cases) classifies each item as running, stalled, parked, done, or done on assumptions, from plain facts. It gives one short line, short enough for a thread title (`chief · N running · N stalled · N parked · N done · N assumed · tick HH:MM`), and one longer summary that names the age of the last tick and each stalled and parked item with its reason. Whether a running worker is stalled is `worker_health.silence`'s call, not a second judgement in the heartbeat.
- **A sink is where the heartbeat shows.** `heartbeat.Sink` is the seam, and `heartbeat.deliver` hands the status to every sink. A sink that fails is reported as `name: reason` and never stops the other sinks or the engine. The engine delivers after each pass and writes each failure to its engine log.
- **The file sink is the one built.** `heartbeat_store.FileSink` writes the status to the engine state directory, and the board and the report read it there. A T3 thread-title sink (form A) and a pinned-board sink (form B) plug in at the same seam. Neither is built, and both need the engine's MCP client, which waits on the owner's approval (section 19). Until then the file carries the heartbeat, which is the design's fallback when the rename fails.
- **The report page.** The page server renders `/report` (`heartbeat_page.py`) from the delivered file. It lists done, done on assumptions, and parked, each item linked to its worker's thread or, without one, to its issue page, under the heartbeat's line and summary. It reads and never acts.

```
St := the heartbeat status      Sk := a sink      F(Sk) := Sk failed to deliver St
Ot := the other sinks still receive St      En := the engine pass continues      Lg := F(Sk) is written to the engine log
Fs := the file sink delivered St      Rp := /report and the board show St
1.  ∀Sk: F(Sk) → Ot ∧ En ∧ Lg      [#1179: heartbeat.deliver; the engine's beat after each pass]
2.  Fs → Rp                         [#1179: the page server reads the file the file sink wrote]
∴   a failing title or board sink never hides the heartbeat that the file sink delivered (1, 2)
```

The stall watcher is a small, non-model watcher in the engine (call, T3 design §10). It is not in the page server, which records and never acts, and not in the coordinator, which stays on prioritization and execution.
- **Stalled** means a worker's item count and update time have stopped moving past a threshold, after a startup grace period. The grace period defaults to three minutes and is configurable; the thresholds are the builder's. As built, one setting, `[workers.stall] after_minutes` (default 3), is both the grace period and the silence threshold.
- **Waiting is not stalled.** A pending request count above zero is a worker waiting: the coordinator answers it as section 17 describes, and it counts as a stall only when it stays unanswered past a threshold. A worker waiting on an architectural decision or a split trio waits without expiry, because those waits are section 15's own.
- **Silence is suspected, never proof.** A worker whose liveness is unknown, or whose thread could not be read, is not judged stalled.
- **Recovery follows section 15's outcomes** (call), and a relaunch after a stall counts as a failure. `[workers.stall] action` is `wait` (the default) or `restart`. With `restart`, the engine stops a stalled local process, releases its slot only once termination is verified, and relaunches it, up to `max_restarts` (default 2) per issue. The stalled attempt is recorded as a failure, and `[workers] failure_limit` (default 2) failures on an issue park it.
- **A T3 thread is not restarted.** A stalled worker launched through T3 is treated as `wait` even when `restart` is configured, because the `t3` CLI has no cancel or interrupt (section 13). It stays a suspected stall until the owner approves a cancellation mechanism (section 19).
- **The judgement is pure.** `worker_health.stall` (Use Cases) sits beside `worker_health.silence`, the stall judgement already there, and is fed the worker's evidence as plain facts (liveness, last activity, pending requests and since when, a decision wait). The engine gathers the facts, acts on the verdict, and the page server displays it (architect, by section 1: one stall judgement, not two).

```
Ic := the item count and update time have stopped moving past the threshold      Gp := the startup grace period has passed
Pr := the pending request count is above zero      Pu := a pending request stayed unanswered past its threshold
Dw := the worker waits on an architectural decision or a split trio      Uk := the worker's liveness or thread state is unknown
St := the worker is stalled      Wt := the worker is waiting      Rc := recovery follows section 15
Rs := [workers.stall] action is restart      T3 := the worker was launched through T3      Rl := the engine stops and relaunches it
Mx := the issue reached max_restarts      Fl := the stall counts as a failure

1.  Gp ∧ Ic ∧ ¬Pr ∧ ¬Dw ∧ ¬Uk → St      [call, T3 design §10; worker_health.stall]
2.  Pr ∧ ¬Pu → Wt ∧ ¬St                 [call, T3 design §10: waiting is not stalled]
3.  Pr ∧ Pu → St                        [call, T3 design §10]
4.  Dw → Wt ∧ ¬St                       [section 15: an architectural or split-trio wait is not a stall]
5.  Uk → ¬St                            [#1163: silence is suspected, never proof]
6.  St → Rc                             [call, T3 design §10]
7.  St ∧ Rs ∧ ¬T3 ∧ ¬Mx → Rl ∧ Fl       [#1163: bounded local recovery; section 15: a stall relaunch is a failure]
8.  St ∧ T3 → ¬Rl                       [#1163: the t3 CLI has no cancel; section 13]
∴   every stall is detected without a model turn and recovered by the unattended outcomes; a waiting worker is answered, not relaunched; and a T3 worker is held as waiting until a cancel exists (8)
```

An unattended run ends with a report to the owner: done, done on assumptions, and parked (architectural, failed twice, or a split trio, with what was tried). `/report` is that report's page. An append-only action log backs it, and the engine's store is its natural home; where it lives is the builder's. The mockup `design/since-you-last-looked-laptop.html` appears to be its surface, and the builder confirms it. The engine records no marker today for an architectural park, a split trio, or an assumption the trio made, so the heartbeat infers each from other records (`compliance.md` row C32).

```
H := the owner returns after being away      P := significant progress was made      S := the owner learns of a stall late or never
X := the integration counts as a success
1.  X ↔ (H → P) ∧ ¬S        [owner, 2026-10-29: the two failure tests]
∴   the heartbeat, the stall watcher and the report exist to make ¬S hold
```

## 19. Open items of the T3 design

These are unsettled. Nothing in this document settles them, and work that depends on one waits for the owner's answer.
- The definition of success, which also blocks auto mode (section 9).
- The names of modes beyond auto, deferred until the system is tried (section 9).
- Deep-link behaviour in the real app, which needs a click in the desktop app (section 18).
- Interrupting and renaming a thread from outside T3. The `t3` CLI has neither; both are MCP tools that need an outside-agent login and an MCP client in the engine (section 18, heartbeat A; and cancel, section 13). That client is a new dependency, so it is adopted only on the owner's approval.
- A cross-project meta dashboard, deferred (section 12).
- Dependencies between issues in different projects, which section 12 leaves uncovered (`compliance.md` row C29).
- Whether the worker guard should deny destructive and credential commands it allows today. On main at 11c445c5, `guard_commands.denial_reason` gives no reason for `git reset --hard`, `git clean -fdx`, `rm -rf /` or reading `~/.ssh/id_rsa`, so a Claude worker may run each. Section 15 keeps the guard as automation that confines merges, protected pushes and stage writes to the engine, and removes every guard rule whose job is a hard stop. Neither settles whether denying a destructive or credential command is a hard stop or a safety check, so this is the owner's question, not a decision (sections 13 and 15).

Answered by the spike notes of pull request 1188 (#1178, T3 0.1.3) and now stated in section 13: named-agent launch parity (the agent's prompt and tools apply, the rotation entry's model wins); how a T3 worker reaches the engine's report and ask server and which runtime mode it runs under (in-tree `.mcp.json`, T3's default `auto` mode); and how T3's worktrees and the engine's reconcile (each engine worktree is registered as a T3 project). Two spike points stay untested: a plugin-qualified agent name in the tree's `agent` setting, and an agent whose `tools:` list must also admit the engine's MCP tools.

Decided but not built yet, after #1178. Each is required by the section named, and the code does not meet it yet:
- Live messages to a T3 worker: `worker.deliver_messages` fails them as "started without live messaging"; `t3 thread send` would carry them (section 13).
- Thread links on the issue pages and the workers page; only the board links the thread (sections 13 and 18).
- Mapping the engine's `[workers] claude_as_profile` to a T3 provider instance; every T3 worker runs under T3's default `claudeAgent` (sections 10 and 13).
- Codex, Cursor and Antigravity under T3; only Claude entries launch through it, since each other runtime needs its own channel file there (section 13, call).
- Cancelling the thread itself; cancel stops the runner, not the thread, until the MCP client above is approved (section 13). A stalled T3 worker is therefore held as a wait (section 18; `compliance.md` row C33).
- The heartbeat's thread-title and pinned-board sinks (section 18; `compliance.md` row C35).

Built since: a T3 worker's liveness and stall evidence come from its thread (`t3_adapter.observe` reads the thread's status, update time and pending request count), not from the runner process. A thread that cannot be read is unknown evidence, never a stall (sections 13 and 18).

```
Mc := an MCP client is added to the engine      Ap := the owner approved it as a new dependency
In := a thread can be interrupted from the engine      Rn := a thread can be renamed from the engine
1.  Mc → Ap          [the owner's rule: every new library, package or tool is confirmed before adoption]
2.  In ∨ Rn → Mc     [#1178: the t3 CLI has no interrupt or rename; both are MCP tools]
∴   In ∨ Rn → Ap: neither the thread cancel nor heartbeat A's rename is built before the owner approves the client
```

```
Dc := the command is destructive or reads a credential (git reset --hard, git clean -fdx, rm -rf /, reading ~/.ssh/id_rsa)
Gd := the worker guard denies it      Hs := denying it is a hard stop      Ow := the owner has answered
1.  ¬Gd for each Dc today        [guard_commands.denial_reason on main at 11c445c5]
2.  Hs → ¬Gd                     [section 15: guard rules whose job is a hard stop are removed]
3.  ¬Hs → Gd is permitted        [section 15: the guard stays as automation]
4.  neither Hs nor ¬Hs is stated [sections 13 and 15]
∴   whether Gd should hold for Dc waits on Ow; until then the guard stays as built (1)
```
