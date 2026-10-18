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

The owner decisions in the 1.0 ledger (`docs/specs/2026-10-09-chief-of-stuff-1.0-ledger.md`, section "Owner decisions", 2026-10-10) amend this document. Where a section here disagrees with one of them, the owner decision stands and the section is rewritten to match. A module the ledger retires, such as the file mailbox, the tracker parser and its command lines, the flow tick, the notify service, the terminal launchers or the non-Claude watcher, keeps its circle in this table until it is deleted. A gap against a retiring module closes when the module is deleted.

New work goes on the engine path: the store, the forge adapter, the model port, the reconcile pass and the pages built on them. The goal is 1.0, then 1.1, with as much legacy dropped as possible (owner, 2026-10-16), so getting the engine to run the workspace comes first. A retiring module takes only the fixes today's loop needs to keep running until that cutover. A new behaviour lands on the engine path. It also lands on a retiring module only when the loop needs it before the cutover, and then the engine side is in the same change or filed as its own issue. A change that adds behaviour only to a retiring module is a gap against this section.

| Circle | Modules |
|---|---|
| Entities | `tracker.py`, `md.py`, `clock.py`, `runtimes.py`, `policy.py`, `findings.py`, `ownership.py`, `orphans.py`, `estimate.py`, `tracker_log.py`, `tree_state.py`, the finding types of `audit_tasks.py`, the message format of `inbox.py`, `guard_commands.py` (the worker guard's pure shell-command judgements), `limits.py` (runtime limit readings and their judgements, section 10) |
| Use Cases | the ownership rules and judgements of `audit_tasks.py`, `dispatch_prompt.py`, `one_shot.py`, `kanban.py`, `merge_ready.py`, `merge_approved.py`, `review_threads.py`, `task_forge.py`, `model_port.py` (the model port), `issue_gate.py` (issue first, then one store row), `trio_gate.py` (the decision trio, design 5a), `decision_actions.py` (a board decision's lifecycle: answer, reverse, re-route, and an answered decision whose action is blocked. The page server only validates and records a person's intent through it; the engine alone applies intents, see section 6), `engine_merge.py` (executes a planned merge, logged on the issue), `forge_port.py` (`ForgeError`, the forge failure boundary), `reconcile.py` (the engine's planner: what the state calls for), `readiness.py` (the Ready predicate over a pass's facts: file-ownership conflicts, dependency blockers and stop records), `completion.py` (judgements on a finished or abandoned run: done-on-main, the read-only rule, unlanded work and orphan recovery), `guard_rules.py` (the worker guard's Stop, permission-relay and session-start rules), the ready-task and stagnation judgements of `flow.py`, `performance.py`, `worker_health.py` (a worker's six health signals and the stall judgement, section 8) |
| Interface Adapters, presenters | `fragment.py`, `columns.py`, `panels.py`, `gantt.py`, `flow_chart.py`, `render_board.py`, `decision_page.py`, `issue_page.py`, `source_page.py`, `module_graph.py`, `workers_page.py`, `page_reload.py`, and the page module of section 3 |
| Interface Adapters, gateways | `store.py` (the issue-keyed store behind the ORM, and its `migrations/`), `forge.py` (the one forge adapter for GitHub and GitLab), `backlog.py`, `backlog_ref.py`, `board_sources.py`, `session_logs.py` (each runtime's session log, found and read into one shape), `workspace.py`, `settings.py`, `tracker_read.py`, `tracker_write.py`, `forge_review.py`, `one_shot_report.py`, `tree_claims.py`, `slots.py` (the worker concurrency cap, E21), `limits_history.py` (the account-wide limit readings behind the ORM, section 10), the git and forge fact gathering of `audit_tasks.py`, the mailbox storage of `inbox.py` |
| Frameworks and Drivers | `claude_adapter.py` and the other host adapters (section 13; host adapters are the only modules that spawn a host CLI), `git_trees.py`, `git_view.py`, the command line of `inbox.py`, the audit orchestration and command line of `audit_tasks.py`, `notify.py`, `notify_service.py`, `supervisor.py` (the engine venv and LaunchAgent), `startup.py` (the engine and `work` commands' workspace settings and forge), `store_access.py` (the migrated store, under the migrate lock), `kanban_cli.py` (the `kanban` command line), `pipeline_cli.py` (the `pipeline` command line: a pipeline's status, or a branch's head and latest pipeline, read through `forge.Forge`), `process_status.py`, `session_exec.py`, `spawn_session.py`, `probe_health.py`, `pages.py`, `chief_of_stuff.py`, `start_coordinator.py`, `hooks/hooks.json`, `board_guard.py`, `init_workspace.py`, `install_model_guidance.py`, `make_worktree.py`, `migrate_backlog.py`, `one_shot_result.py`, `shell_setup.py` (the login shell, and the only place the workspace's `[workers] teardown` and `teardown_list` commands run; Use Cases receive teardown as a passed-in function, never by import), `cleanup.py` (engine-owned worktree removal behind every finding-27 guard, E19; it runs the workspace teardown in a tree before its container-mount guard), `engine.py` (the workspace singleton loop that executes the reconcile plan), `engine_facts.py` (gathers one pass's git, worktree, process and forge facts into plain records for the planner), `work.py` (the issue-first intake command line), `worker.py` (worker launch through the adapter and completion collection), `worker_guard.py` (the worker guard's hook entry point), `guard_binding.py` (the worker's binding read from its environment), `engine_mcp.py` (the engine's report and ask MCP server), `guard/hooks/hooks.json`, `adopt_one_shots.py`, `sync_page_theme.py`, the command line and tick orchestration of `flow.py`, `performance_report.py`, `limits_probe.py` (unbilled reads of each runtime's limits, section 10), `limits_cli.py` (the `limits` command line) |

## 5. Enforcement

The dependency rule of section 1 is enforced by `evals/test_architecture.py`. It holds the circle table of section 4 and fails on any import from an inner circle to an outer one. Imports that violate the rule today are listed in that test by name, the list may only shrink, and each entry is also a gap in `compliance.md`.
## 6. Reviewers

Review is policy, as the 1.0 ledger settles (D7, F10, F12, F13; plan M4). Personas (role, model, effort, rules), rooms, stages, and the review policy are data: `assets/policy.toml`, read and validated by `policy.py` (Entities). Which personas review a stage, what counts as an open finding, the retry limits, and when the architect is consulted live there, with defaults per persona that a stage or room overrides.

The engine runs the review panel.
- `reconcile.py` (Use Cases) plans one review launch for each persona the policy names for the stage, and they run in parallel.
- It plans a fix for every open finding, so review burns down to none.
- It routes findings to the architect when the policy says to: the engine posts a request and waits, and the architect disposes of each finding as fix, file, keep or discard.
- It pings after three fix rounds.
- `engine.py` executes the plan through the model port.

A review is bound to the head it reviewed: the segment records the reviewed commit, and a merge happens only when that commit is the change's current head.

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

The engine never starts an architect run on its own (owner, 2026-10-17). The architect is a standing session that keeps its context across consults.
- **A consult is a request.** The engine posts it where the architect watches, on the change or its issue, in the form `Architect review requested: <sha>`, with the findings to dispose of. It then waits for the architect's reply on the same thread, and treats the issue as waiting on the architect until the reply comes.
- **The engine reads the reply as data:** `Architect: APPROVED <sha>` or `Architect: CHANGES <sha>`, plus one disposition per finding.
- **The head is the forge's.** "The current head" is the change's head as the forge adapter reports it, never a worker's local `HEAD`.
- **Only the approval binds to the head.** An approval counts for that head only, so a new head needs a new approval and one new request.
- **Dispositions persist.** A finding disposed as fix, file, keep or discard stays disposed across heads, keyed by its finding. A request for a later head carries only the findings not yet disposed, and a `CHANGES` reply whose findings are all disposed needs no further request until the head moves.
- **A new architect run happens only when the owner asks for one,** naming the architect in the request. Nothing in the policy, the planner or the trio launches the `frank-lloyd-aight` agent unprompted.

The decision trio's models are not fixed (owner, 2026-10-18; issue #879). Its three personas keep their roles and judgement lenses from the policy, but each consult draws its models the way workers do.
- **A rotation class.** The workspace's `[models.trio]` rotation, with its `pick` (`first` or `random`), supplies the model for each trio member. A persona's own `model` is only the fallback when no rotation is set.
- **Three different models.** A consult draws three distinct launchable entries, preferring different runtimes or model families, so a unanimous verdict means three independent judgements, not one model asked three times. If fewer than three distinct entries can launch, it uses what it has and records that the trio was not fully diverse.
- **Every judgement is recorded.** Each member's vote names the runtime and model that cast it, so trio accuracy can be compared across models over time, as for workers (section 9).
- **Consults are read-only on every host.** A trio consult is a judgement call through the model port. It runs in each host's read-only or no-tools mode (Claude's `--safe-mode`, a read-only sandbox elsewhere), never with write access. A host that offers no read-only mode is not launchable for the trio.
- **Quota.** A quota stop moves that member to the next distinct entry. When no entry can run, the decision falls back to the owner as an exhausted retry.

```
Mt := the trio's models come from [models.trio] (persona model only as fallback)
D := the three members use distinct entries, preferring different runtimes or families
U := a unanimous verdict reflects independent judgements      R := each vote records its runtime and model
O := every consult runs read-only on its host

Mt ∧ D → U          [owner 2026-10-18: "trio shouldn't be fixed models"]
R → trio accuracy is measurable per model (section 9)
O                   [a judgement call never writes]
∴ the trio rotates like workers, stays diverse, and stays read-only
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

Each module keeps re-exporting the names its callers import until they import from the new modules. Where the module boundaries and their names fall is decided when the work is assigned. On the engine path, `flow.py`'s ready-task judgement lands in `readiness.py`, and its stagnation judgement lands in `worker_health.py`, beside the silence signal it shares a rule with. The audit's done-on-main, read-only, unlanded and orphan judgements land in `completion.py`.

On the engine path, file ownership is declared on the issue, because the forge is the source of truth and the store rebuilds from it.
- **Where it is declared.** An issue states the paths it owns and their mode (write, or read-only) in a section of its body.
- **Who reads it.** `ownership.py` (Entities) parses that section, the same reader that parses the tracker's File ownership rows.
- **Who writes the store rows.** The engine's forge sync in `engine.py` writes them into `store.Ownership` each pass, and so does intake through `work.py` and `issue_gate.py`. `engine_facts.py` reads them back for the planner.
- **Workers declare nothing.** They report what they changed, and `completion.py` judges that report against the issue's declared scope. A read-only run that changed anything is held for human review, with the paths named. They never grant themselves a scope.
- **The engine handles a write outside the declared scope by judgement, with no automatic human hold** (owner, 2026-10-17). Real fixes often touch a test, a README or a shared helper nobody listed, but a change with no reason to be there is a defect.
  - **Widen.** When a write run changed paths outside its declared write paths, the engine widens the issue's scope to what the run actually changed. It writes the widened scope to the issue's `## File ownership` section and to the store, so later readiness checks use the truth.
  - **Judge.** The review panel judges each out-of-scope path against the issue's outcome. The engine hands it the list of paths as an explicit check, so no extra model call is needed. A path the change needs, such as its test, its documentation or a helper it uses, is justified and stays. A path with no reason in the issue is a finding of at least Medium severity, "unjustified change outside scope", and it is fixed like any other: the fix stage reverts it, or the PR shows why it belongs. The architect's dispositions apply. A person is involved through the existing escalations: the three-round ping, or human review when the panel or the architect rules that a person must decide. The scope narrows again once a revert lands.
  - **No overlap.** If the widened scope overlaps no other in-flight issue's write scope, the run proceeds to review normally.
  - **Overlap.** If it does overlap, the engine orders the two issues' merges, so the one that finishes first lands first. The other rebases onto it and is reviewed again on its new head, which the merge gate already requires (a review of the current head, then main's CI). A conflict the rebase cannot settle goes back to the fix stage like any other finding, and a person is involved only through the existing three-round ping.
  - **No new dispatch.** While the overlap lasts, readiness counts the widened scope, so it dispatches no new issue into those paths.
- **Every issue declares its scope.** An issue whose `## File ownership` section is missing or malformed is not dispatched: readiness holds it as "needs file ownership", and nothing is inferred for it. The whole repository is never a default. An issue that truly needs it declares it explicitly, with the path `.`, and then conflicts with every running write in that repository (owner, 2026-10-17).
- **Intake writes the section.** `issue_gate.py` refuses to file an issue without a valid section, and the repository's issue forms carry it as a required field, so an issue made by hand declares its scope too. Open issues filed before this rule get the section added before the engine takes over dispatch.

```
I := an issue      S(I) := I has a valid File ownership section
W(I) := I declares the whole repository, with the path "."
D(I) := I may be dispatched      R(I) := I conflicts with every running write in its repository

∀I: ¬S(I) → ¬D(I)      [owner 2026-10-17: never allow the whole-repo fallback generally]
∀I: W(I) → S(I) ∧ R(I)  [the whole repository only when declared]
intake refuses ¬S       [issue_gate.py; the issue forms]
∴ the engine serializes a repository only for an issue that asked for the whole of it, and an undeclared issue waits instead of blocking everyone

O(r) := run r changed only paths inside its declared write scope
Wd(r) := the engine widens r's issue scope to the paths r changed (issue section and store)
X(r) := the widened scope overlaps another in-flight write scope
Q(r) := the merges are ordered and r is re-reviewed on its rebased head
H(r) := r is held for human review
J(p) := out-of-scope path p is justified by the issue's outcome (judged by the review panel)
F(p) := p is a Medium+ finding, fixed by reverting it or justifying it in the PR

∀r: ¬O(r) → Wd(r)              [owner 2026-10-17: the orchestrator handles it]
∀p: J(p) → p stays             [a test, docs or a helper the change needs]
∀p: ¬J(p) → F(p)               [owner 2026-10-17: no excuse → rework, or human review]
H(r) only through the existing escalations   [the three-round ping, or a panel or architect ruling]
∀r: Wd(r) ∧ X(r) → Q(r)        [merge gate: review of the current head, then main's CI]
∀r: Wd(r) → readiness uses the widened scope   [no new dispatch into those paths]
∴ justified edits flow through; unjustified ones are reworked, or go to a person; no parallel run is overwritten unnoticed
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

What a workspace builds, deploys or runs, and how, reaches chief-of-stuff only through that workspace's own configuration: the `## Coordinator` block (its human-only actions, `Health:` lines and the rest) and the Settings TOML (such as `[workers] teardown` and `teardown_list`). chief-of-stuff runs those commands as given and never interprets them. A rule or a fixture that needs an example of a workspace-specific action uses an invented name, never a real product.

Two things are chief-of-stuff's subject, not a stack, and stay named: the host CLIs it coordinates (claude, codex, cursor and agy, in `runtimes.py`), and the forges it integrates with (GitHub and GitLab, behind `forge.py`). The repository's own CI tooling under `.github/` is how this project is tested, not something chief-of-stuff runs, and is outside this section.

## 12. Parent and child workspaces

One engine can work several workspaces at once (owner, 2026-10-17). A parent workspace lists child workspaces, and one engine runs them all. Issue #825.
- **Children are unchanged.** Each child is a full workspace: its own `CLAUDE.md` (Coordinator and Architecture blocks), backlog and forge, store, review policy, settings and file-ownership rules. Everything sections 4 to 11 say of a workspace holds for each child on its own.
- **The parent adds one table.** `[workspaces]` in the parent's settings maps a child's name to its path, relative to the parent root. A parent with its own Backlog is a workspace too, and is worked alongside its children. A child that cannot be read is reported and skipped, never fatal.
- **One engine, one pass per child.** `chief-of-stuff engine --root <parent>` runs the per-workspace pass once for each child in every loop. The pass is reused unchanged. A failure in one child is logged in that child's store and never stops another.
- **One slot pool.** The parent owns a slot pool that caps running workers across all children at its `[workers] max_concurrency`. Each child's own `max_concurrency` still caps that child. Runtime limits are already per account (section 10), so a near-cap judgement covers every child on that account.
- **One engine per workspace still holds.** The parent engine takes each child's singleton lock. A child already run by its own engine is skipped with a warning. Installing a child's own supervisor while a parent runs it is refused. The parent has one supervisor service.
- **One board index.** The parent board lists each child's board. Each child keeps its own board.
- **Not covered here:** dependencies between issues in different children. They wait for a need.

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

## 13. Host adapters

Engine workers can run on Claude, Codex, Cursor and Antigravity (owner, 2026-10-17, pulling #700 and #701 forward). Each host is an adapter behind the model port, as design decision 7 intended.
- **One contract.** Every host adapter implements the same start, cancel and classify contract as `claude_adapter.py`, and writes the same run evidence in the run directory: `command.json`, the spawn record, `started.json` and `exit.json`. The engine, `worker.py` and recovery see no difference between hosts.
- **The registry builds the command.** Each host's argv, prompt bootstrap and environment come from its `runtimes.py` entry (Entities). No adapter imports the one-shot launcher, which retires (#830). Process plumbing shared by the adapters lives in one place (DRY). Each host's argv and error classification stay in its own entry or adapter (SOLID).
- **Outcomes reach the engine through the report tool.** The engine's `report` and `ask` MCP server is registered for every host at launch, with the same binding, and its configuration lives outside the worker tree. A worker that stops without a report is "stopped without reporting an outcome", whatever the host.
- **The guard holds on every host.** The Claude plugin's PreToolUse gate does not load on other hosts, so a non-Claude worker gets a host-neutral guard with the same judgements:
  - **Shims:** a `PATH` shim directory, placed first by the engine, wraps `git`, `gh` and `glab` and refuses what `guard_commands` refuses: merges, pushes to protected or ambiguous refs, and forge calls outside the allowlist. It reuses `guard_commands` rather than copying it. A shim, like the hook, is a guard rail. The forge and the engine are the boundary.
  - **Branch protection:** a non-Claude entry launches only in a workspace whose forge protects its default branch and the branch its requests target, which can differ, for example `main` and `develop`. Otherwise the worker moves to the next entry in its rotation.
  - **The engine checks after the run, on every protected branch.** It records each protected branch's head when a non-Claude run starts. At collect, every first-parent commit a protected branch gained during the run must be a merge the engine recorded, as structured data (request and merge commit), never inferred from log text or a time window. Any other commit, whether a worker's merge or a direct push, holds the run for human review, naming each commit and its branch. An unreadable branch holds too.
- **Quota and outage** are classified per host into the model port's retryable kinds, never by guessing at success.

```
H := a host adapter implements the model port's contract and run evidence
R := argv and environment come from runtimes.py      G := the shim reuses guard_commands' judgements
B := the forge protects the default branch and the request target branch
Rm(c) := commit c is a merge the engine recorded      P := every protected branch
N := a non-Claude worker may launch      Ok := the run passes the post-run check

H ∧ R            [design decision 7; #830 retires the one-shot argv]
N → G ∧ B        [the guard holds on every host; an unprotected branch means the next entry]
Ok ↔ ∀b ∈ P, ∀c ∈ first-parent(start(b)..end(b)): Rm(c)      [unreadable → ¬Ok; #860]
¬Ok → held for human review, each commit named, never silent
∴ other hosts run behind the same port, the same evidence and an equivalent guard
```
