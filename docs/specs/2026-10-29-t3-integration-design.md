# Chief of stuff in T3: design

Status: draft for the owner's approval. Written 2026-10-29 from a brainstorming session. Nothing here is built. The owner's decisions are marked as decided; everything the author chose is marked as a call and listed again in section 12; everything unproven is listed in section 13.

## 1. Intent

The owner wants to hand a project to chief of stuff, walk away, and come back to significant progress, with no surprise stalls. The place to do that is T3 Code, the agent harness app the owner already works in, because T3 gives every worker a thread the owner can open, read and message, and gives the engine the visible hub it lacks today. The owner's own words for the problem: they wonder whether the engine is working "all the time".

The model is the Gas Town one: a coordinator (the mayor) delegates to short-lived workers, and the owner can step into any thread at any time. The owner stays at the decision level: architecture, and anything the agents cannot settle themselves.

```
H := the owner returns after being away          P := significant progress was made        S := the owner learns of a stall late or never
X := the integration counts as a success
1.  X ↔ (H → P) ∧ ¬S        [owner, 2026-10-29: the two failure tests]
∴   the design is judged by what the owner finds on return, not by features shipped
```

## 2. Goals, non-goals and anti-goals

Goals:

- The headline scenario works end to end: an architecture session produces the architecture, issues are created on the forge, work runs unattended, and the owner returns to progress.
- Every worker is a T3 thread the owner can open, read and message.
- The existing dashboards, board, decisions, per-issue pages and flow charts, are visible in T3, and each task links to its worker's thread.
- Stalls without a good reason recover on their own. The owner hears about every stall that did not recover.
- Every choice about workers is configurable.

Non-goals (decided):

- No cross-project dashboard now. A meta dashboard that spans projects is deferred.
- No routing by success rate yet. The rates are recorded only.
- Not a replacement for the forge, which keeps issues and pull requests, or for the architecture documents.
- Other people using the project is in scope, because the project is open source. So nothing in the design hardcodes one person's agents, models or accounts; configuration carries them.

Patching T3 (decided, owner, 2026-10-29: "last resort"): the default is no. A patch is allowed only after a live test shows that a needed capability, most likely the pinned board panel of heartbeat B, cannot be had from outside. It is then as small as possible and kept as a patch file outside the T3 tree.

```
Nd := a capability the design needs      Ob := it can be had from outside T3      Lt := a live test showed that it cannot      Pt := T3 is patched
1.  Pt → Nd ∧ ¬Ob ∧ Lt      [owner, 2026-10-29: "last resort"]
2.  ¬Lt → ¬Pt               [from 1]
∴   no patch is written on a guess; heartbeat A stands alone until the test is run
```

Anti-goals, the two failure tests, decided: the owner runs an architecture session, issues are created, the owner walks away, and on return sees no significant progress; and the owner does not know that something stalled.

## 3. Constraints

- Plugin-minded. T3 has no shipped plugin API. A plugin view proposal exists upstream and is not merged. The integration drives T3 from outside and does not fork it, so the owner is not reconciling T3 changes. A patch to T3 is a last resort and as small as possible.
- Plugin-minded means chief of stuff stays its own component: the engine, the tracker, the page server and the agents.
- One architecture agent per project, one coordinator per project, and a meta coordinator over projects. The coordinator is the project's mayor.
- Only the architecture agent changes the architecture directory. Where this design needs a change to `architecture/ARCHITECTURE.md` or `architecture/compliance.md`, the builder reports it to the architecture agent and does not edit those files.
- Rules set outside chief of stuff, in a user's or host's own settings, are unaffected by this design.

## 4. Roles

| Role | Lifetime | Job | Who talks to it |
|---|---|---|---|
| Meta coordinator | Standing | Prioritizes across projects. An agent thread on the parent workspace of section 12 of `ARCHITECTURE.md`, which today has an engine pass and a slot pool but no agent (call). It reads the children's trackers and orders work across them. | The owner |
| Project coordinator | Standing | Prioritizes and executes within one project. Routes work to workers. Answers worker questions. | The owner, the architect, workers |
| Architecture agent | Standing, one per project | Keeps the canonical architecture documentation true, plans, checks drift. Changes no code. | The owner, the coordinator |
| Issue-filing worker | Short | Files forge issues from a settled architecture or a drift document, then ends. | None |
| Implementation worker | Short | Picks up an issue under the coordinator's rules and fixes it. | The coordinator, the owner |
| Reviewer, other workers | Short | As today. | The coordinator |

Decided: every role above is a T3 thread. The owner talks to the coordinator about coordination and to the architect about architecture, and can step into any worker. Handoffs between the standing roles are automatic. The owner sometimes directs a handoff by hand.

There is always exactly one architecture agent session per project (decided). It can be asked to do tasks, and it has its own rules for spinning off subagents. Those subagents are its internal matter: they are not workers of the coordinator, they are not in the worker rotation, and their runs are not in the success records (call).

The architecture agent has three core responsibilities (decided): keeping the documentation canonical, which is also the first job on a new project because the documents have to be written before they can be kept true; planning; and drift checking. The canonical set is the project's architecture directory, every file in it, not one document. Anything outside that directory is not canonical.

## 5. Autonomy

Decided: the existing hard stops are removed entirely. That means the coordinator's human-only action list, merges waiting on the owner, the yes-gates between pipeline stages, and the guard hooks and command rules that enforce them. The default is to proceed. Waiting for an architectural decision is not a hard stop and stays. Architectural decisions belong to the owner and the architect, who collaborate under their own rules. An agent that cannot tell whether a decision is architectural asks the architect. The coordinator answers every other question.

```
D := the decision is architectural      U := the work waits for the owner or the architect      H := one of the old hard stops applies
Q := an agent cannot tell whether D     A := the agent asks the architect
1.  D → U            [owner, 2026-10-29]
2.  ¬D → ¬U          [owner: the default is autonomy]
3.  ¬H               [owner: kill the existing hard stops entirely]
4.  Q → A            [owner]
∴   commit, push, merge and deploy proceed unattended unless the decision is architectural
```

Who answers (decided): the coordinator owns scheduling, priority and dispatch, so it answers the questions in that scope. The decision trio answers the non-architectural decisions outside that scope. The trio stays as it is in `ARCHITECTURE.md` section 6: three independent models, read-only, each question asked once.

```
Co := the question is about scheduling, priority or dispatch      D := the decision is architectural
Ac := the coordinator answers it      Tr := the decision trio answers it      Ar := the owner and the architect decide it
1.  D → Ar             [owner, 2026-10-29]
2.  ¬D ∧ Co → Ac       [owner, 2026-10-29: "the coordinator owns scheduling and priority and dispatch"]
3.  ¬D ∧ ¬Co → Tr      [owner, 2026-10-29: "trio for decisions outside the coordinator's scope"]
∴   every question has exactly one answerer
```

Review and merge (decided): the default is motion. The review panel, the fix loop and the merge gate of `ARCHITECTURE.md` section 6 stay, because they are automated checks and not stops. The coordinator merges once the review matches the change's current head and main's CI is green. Where the policy requires the architect's approval, the merge waits for the architect and not for the owner. Human review stays possible and surfaces only on a contradiction, an issue or a problem: three fix rounds with findings still open park the issue with those findings and a recommendation.

```
M := the coordinator merges the change      Rv := the review matches the current head      Ci := main's CI is green
Ap := the policy requires the architect's approval and it is given      Rq := the policy requires the architect's approval
X := a contradiction exists (two current decisions, or a decision and the code)
Fx := three fix rounds ended with findings still open      Hr := the change surfaces for human review
1.  Rv ∧ Ci ∧ (Rq → Ap) ∧ ¬X ∧ ¬Fx → M    [owner, 2026-10-29: "the default should be to try to make motion"]
2.  X ∨ Fx → Hr                            [owner, 2026-10-29: human review surfaces on contradictions, issues or problems]
3.  Fx → Pk                                [call: the old three-round ping becomes a park, which fits section 5 rule 1]
∴   a merge never waits on the owner by default, and the owner sees a change only when something is wrong
```

Consequences for the builder: the coordinator agent file, the guard directory and the engine change substantially. The rule in `ARCHITECTURE.md` section 4 that host adapters are the only modules that spawn a host CLI interacts with the T3 launcher in section 9 and goes to the architecture agent.

Unattended outcomes (decided): every piece of unattended work ends in one of three outcomes.

- Do: no decision is needed. Finish it.
- Do on a default: a decision is needed, a sensible default exists, and undoing it is cheap. Take the default on separable work and log the assumption.
- Park: the decision is architectural, or the work failed twice. Park it with its prep done, which means research, options and a recommendation, so the owner's decision is one answer.

```
Pk := the item is parked      Df := proceed on a default      Do := just do it
D := the decision is architectural      F := the work failed twice
E := a decision is needed, a sensible default exists, and undoing it is cheap
1.  D ∨ F → Pk       [owner, 2026-10-29: parking limited to architectural or two failures]
2.  E ∧ ¬D ∧ ¬F → Df [the owner's do-on-a-default rule; call: two failures park even when a default exists]
3.  ¬(D ∨ F ∨ E) → Do
∴   a stall is justified only by D or F; every other stall is a bug
```

Failed twice (call): the count is per issue, across agents, and a relaunch after a stall counts as a failure.

Retry policy (decided): a failed or relaunched worker goes to the next agent in the rotation. Failure counts against the first agent's record and success against the second's.

## 6. Handoffs

Decided: both flows below use the same short-lived issue-filing worker. The architect never files issues.

1. Drift: the architect detects drift and writes a drift document. The coordinator spawns an issue-filing worker, which files the issue and ends. The issue joins the coordinator's queue and a later worker picks it up under the coordinator's rules.
2. New architecture: the architect marks the architecture, or a section of it, settled, in whatever granularity it chooses. The coordinator spawns an issue-filing worker for what was marked.

The architect plans. The coordinator prioritizes and executes. Whether a fix changes the code or the architecture is the architect's call, and the coordinator only schedules the work.

```
Dr := the architect records drift              Fw := an issue-filing worker is spawned and ends     Iq := the issue is in the coordinator's queue
Se := the architect marks documents settled    Wk := a later worker implements the issue
1.  Dr → Fw ∧ Iq     [owner, 2026-10-29]
2.  Se → Fw          [owner: the architect marks settled; granularity is its call]
3.  Iq → Wk          [owner: another worker picks it up later under the coordinator's rules]
∴   no human step lies between recorded drift or a settled architecture and a fix
```

A channel from the architect to the coordinator does not exist today in either agent definition and is new. The architect currently grades compliance only when told. Call: it grades after each merge to main and whenever a worker or the coordinator reports a mismatch, and it writes gap rows in `compliance.md`. Only the architect writes that file, so the coordinator keeps its own ledger of the row ids it has turned into issues and never edits the rows. Each settled marker and drift document names the commit it was written against, and the issue-filing worker checks the forge for an existing issue with the same key before it files, so a repeated signal files nothing twice (call). The builder decides the mechanism.

## 7. Decision graph and questions

Decided: the coordinator answers worker questions. Its answer source is precedent, which today is the Decisions rows of the tracker, which its definition already tells it to read before asking. The owner wants a decision graph, which does not exist yet, to replace that source. Design the coordinator's answer source as a seam so the graph can be introduced without changing the coordinator.

The graph (decided):

- Leaves only by default. An agent sees the most recent decisions, those nothing has replaced.
- Each leaf carries the full decision text as a usable unit, not a diff, with the reason for it. The reason of a current leaf is always granted automatically.
- Provenance is by links to parent decisions. A leaf may replace several earlier ones, so the structure is a graph and not a strict tree. Rejected alternatives live only in ancestors, never in a leaf.
- History is reached only through an approved tool. The tool is the only way agents reach the graph. The coordinator, the architect and the owner read freely and should not routinely, to protect their context.
- The owner needs a way to view and traverse the graph. Its form is open.
- The purpose is to prevent decision poisoning: superseded decisions staying in a context window and being mistaken for current ones.

History access (decided): a request for ancestors is refused by the tool, with the instruction to justify it. The justification goes to the coordinator, which is harsh and judges the justification text without reading the history. If it approves, the tool returns the slice straight to the worker, as far up as the justification requires and as the coordinator approves. The grant is logged (call).

```
Rq := a worker requests history             J := the request states a justification      Ap := the coordinator approves
Rd := the worker receives the ancestors     Fr := the requester is the coordinator, the architect or the owner
1.  Rq ∧ ¬J → refused, told to justify       [owner]
2.  Rq ∧ J ∧ Ap → Rd, as far up as the justification requires     [owner]
3.  Fr → reads freely                         [owner]
4.  a request meeting no grant case → ¬Ap      [owner: the coordinator is harsh]
∴   history reaches a worker only on an approved, justified request
```

Justification rules, all decided. Grant: two current decisions contradict, and the request states exactly what each requires that cannot both hold; a current decision does not match what the code shows, with the mismatch also reported to the architect. Deny: a reviewer finding that contradicts a leaf, which the coordinator rules on from the leaf and its reason; "more context", "looks related", "the decision is unclear", "double-checking my own work". A request to learn whether an approach was already rejected (owner, 2026-10-29: "grant it as a narrow answer"): the worker names the specific approach it is about to build and why the answer is not obvious. If the coordinator approves, the tool answers only that question: it returns the matching rejected alternative and the reason it was rejected, and no ancestors. A vague request such as "has anything like this been tried" is denied. The grant is logged.

```
Rj := a worker asks whether approach a was already rejected      Nm := the request names a specific approach and why the answer is not obvious
Ap := the coordinator approves      Hit := a rejected alternative matches a      Sl := the tool returns that alternative and its reason, and no ancestors
1.  Rj ∧ ¬Nm → ¬Ap      [owner: the coordinator is harsh; call: a vague request names no approach]
2.  Rj ∧ Nm ∧ Ap ∧ Hit → Sl      [owner, 2026-10-29]
3.  Sl → no other history enters the worker's context      [owner: alternatives do not pollute the context]
∴   a worker learns of a rejected approach without receiving the history around it
```

## 8. Workers and records

Decided: the coordinator fans work out across the owner's configured agent types in round robin, a retry goes to the next agent, and the engine records the success rate of each agent at each type of work. Records only for now.

Today `ARCHITECTURE.md` section 9 records every worker run with its runtime, model, effort and agent type, and its outcome: completed, sent to human review, relaunched, and median minutes. It computes the rates per kind of task, where the kind of task is the issue's workflow. Scoring never blocks a launch. This design keeps that and adds the T3 thread's provider and model as a source of the runtime fields.

The engine already has a runtime rotation: a worker moves to the next entry in its rotation (`ARCHITECTURE.md` section 13), and the decision trio draws models from `[models.trio]` (section 6). The round robin and the next-agent retry extend that rotation and add no new one (call). An agent type here means one configured entry: runtime, model and effort.

Call: the rotation skips an agent whose account is near its limit where limits are known. Today only Claude's limits are read (section 10), so for the others the signal is a failed launch.

Call: the records keep raw outcomes (completed, sent to human review, relaunched, minutes), keyed as today by runtime, model, effort, agent type and the issue's workflow, so any later definition of success can be computed over them.

Decided: every worker choice is configurable, meaning strategy, tuning and launcher. Tuning is set by modes and policies. An auto mode lets the coordinator adjust its own tuning from the results data. The names of any other modes are deferred until the system is tried. The definition of success is to be determined, and auto mode cannot be built until it is.

## 9. Launcher

Decided: how a worker is launched is configurable, per workspace. The current path, a local CLI process, and T3 are two options behind the same host-adapter contract of start, cancel and classify in section 13 of `ARCHITECTURE.md`. If the T3 launcher is an adapter, the rule that only adapters spawn host CLIs does not need bending, and the architecture agent confirms it.

The launcher is how a worker starts and the runtime is which agent it is. T3 is a launcher and not a fifth runtime: claude, codex, cursor and agy workers are launched through it with the provider and model of their rotation entry (call).

What the T3 launcher replaces, from the code survey: spawn, cancel and process-identity checks; the running-workers list built from the process table and session files; liveness by PID; the one-shot wait; the transcript-based activity columns. What it keeps: the assignment text, the engine channel for reports and asks, the store's segments, forge post-checks.

What the T3 launcher uses, and its status:

| Capability | Mechanism | Status |
|---|---|---|
| List, start, show, send threads | `t3 thread` CLI with `--json`, `--provider`, `--model`, `--worktree`, `--branch` | Verified by a spike on T3 0.1.3 |
| Thread state | status, settled, item count, pending request count, update time, parent thread | Verified by the spike for the fields; `pendingRequestCount` seen only at 0 |
| Push updates | The server's WebSocket subscriptions for shell and thread state | Inferred from T3 source, not tested. The MCP tools only poll. |
| Delegation by MCP | `delegate_task` | Needs an active parent run, so the engine's Python cannot use it alone. The CLI or an outside-agent login is its route. |
| Start as a named agent | A dedicated Claude provider instance with launch arguments such as `--agent <plugin>:<name>`, forwarded to the SDK | Inferred from source, not tested. Plugin agents ignore `initialPrompt`, so the first message must carry the start prompt. |
| Plugins, skills, hooks, CLAUDE.md in a T3 Claude session | Normal Claude configuration loading | Inferred from source and observed in a live session |
| Rename a thread | `t3_thread_update` MCP tool with an outside-agent OAuth login | Inferred from source, not tested. The CLI has no rename. |
| Worker isolation | `--worktree` and `--branch` on `thread start`, or `isolation: worktree` on `delegate_task` | Verified constraint: `thread start` takes only a registered T3 project, and the path of one of the coordinator's own worktrees was refused ("No T3 Code project matches"). A T3 worker therefore starts in the registered project and T3 creates the worktree, or the coordinator's worktree must first be registered. Open for the builder. |
| Engine channel for a T3 worker | The engine's report and ask MCP server must be reachable from the thread, and the thread's runtime mode and permissions set | Open for the builder |

The engine's worker identity today is a session UUID and a PID with no URL. The builder adds the T3 thread id to the run record and the session registration, and renders it as a link on the board, the issue pages and the workers page.

## 10. Observability

Decided: the dashboards the coordinator has today are the dashboards in T3. The board, decisions, per-issue pages, flow and Gantt charts keep being rendered by the page server from the tracker and the store, and T3 shows them. Each task links to its worker's thread.

The engine's heartbeat, decided: both of these.

- A. A standing thread whose title carries the engine's status, for example alive time, running count, waiting count. It costs no model turns. It needs the rename path above, which is inferred and untested.
- B. The board pinned in a Browser panel, with a heartbeat age at the top. T3 does not keep a pin across restarts, which is the weakness of B. If the rename path fails, B alone carries the heartbeat (call).

Deep links: from a dashboard page, link to a thread with `t3code://threads/<environment>/<thread>`. Opened from the OS it navigates without a prompt. Opened from a Browser panel it shows an Open/Cancel prompt and hands off to the OS. The `t3-thread://` form is a chat-only chip and does not work from HTML. Not yet tested live. The `<environment>` part is read from T3, whose data directory holds an `environment-id`, and is never hard-coded (call).

Stall detection (a call, built on the verified fields): a small non-model watcher in the engine. It is not in the page server, which only records and never acts (`ARCHITECTURE.md` section 6), and not in the coordinator, to keep the coordinator on prioritization and execution. It decides that a worker is stalled when its item count and update time stop moving past a threshold, after a startup grace period. The spike's trivial run took about 50 seconds, so the default grace period is three minutes and configurable. A pending request count above zero is a worker waiting, not stalled: the coordinator answers it as in section 7, and it counts as a stall only when it stays unanswered past a threshold. Recovery follows section 5, and the page server displays the watcher's result.

Unattended runs end with a report to the owner: done, done on assumptions, and parked (architectural, or failed twice, with what was tried). The design mockup `design/since-you-last-looked-laptop.html` appears to be the surface for it, and the builder confirms. An append-only action log backs the report.

## 11. Engine rework

Decided: the engine is substantially reworked to run work autonomously as far as possible, launching and watching workers in T3 through the CLI and server interfaces, with the CLI launcher kept as a configurable alternative. The problem it solves, in the owner's words: there is no central obvious hub to tell it is working.

Existing behaviour this design changes or touches. The architecture agent reconciles these in the architecture documents, and those changes land before the code that depends on them, because implementation must meet the specification (call).

- `ARCHITECTURE.md` section 6 says a new architect run happens only when the owner asks, and that nothing launches the architect unprompted. This design makes the architect a standing session that grades and signals unprompted.
- The decision trio of section 6 keeps its role for decisions outside the coordinator's scope (section 5). The coordinator answers scheduling, priority and dispatch questions, which is new, and the architect-only-on-request rule of the same section is changed above.
- The coordinator's human-only list, the lane gates, merges waiting on the owner, and the guard that enforces them are removed. The review and merge stages that remain are set in section 5; the pings after three fix rounds become parks.
- The branch-protection precondition for non-Claude entries (section 13 of `ARCHITECTURE.md`) stays, because it is a launch precondition and not a human gate (call).
- The page server still records and never acts. Nothing here changes that.

## 12. Calls made by the author

- The architect grades after each merge to main and whenever a worker or the coordinator reports a mismatch.
- A drift trigger channel from the architect to the coordinator, through `unassigned` gap rows, is the intake.
- The rotation skips an agent near its limit.
- An approved history request is limited to what the coordinator approves, and every grant is logged.
- The meta and project coordinators map to the parent and child workspaces of section 12.
- The stall watcher design and its thresholds.
- The stalled-thread recovery follows the three outcomes of section 5.
- The meta coordinator is an agent thread on the parent workspace.
- Two failures park an issue even when a default exists; the count is per issue and a stall relaunch counts.
- The rotation extends the engine's existing rotation, and T3 is a launcher, not a fifth runtime.
- A repeated architect signal files nothing twice, and the coordinator keeps its own ledger of the gap rows it has acted on.
- The stall watcher lives in the engine and the heartbeat falls back to the pinned board.
- Architect subagents are outside the rotation and the records.
- Architecture-document changes land before the code that depends on them.

## 13. Open items

- The definition of success, which also blocks auto mode.
- Names of modes beyond auto, deferred until tried.
- The view for traversing the decision graph.
- Deep link behavior in the real app, which needs a click in the desktop app.
- Named-agent launch parity: whether the agent's model, tools and initial prompt apply under T3's own session options.
- Rename from outside, which needs an outside-agent login and an MCP client in the engine.
- A cross-project meta dashboard, deferred.
- Dependencies between issues in different projects, which section 12 of `ARCHITECTURE.md` leaves uncovered.
- How a T3 worker reaches the engine's report and ask server, and what runtime mode it runs under.
- How T3 worktrees and the coordinator's worktrees reconcile.

## 14. Left to the builder

- The mechanism for the architect's signals: files, thread messages, or both.
- The storage and schema of the decision graph, and the tool's interface, within the rules of section 7. A justified request must reach the coordinator and its verdict must reach the blocked worker without the worker polling in a model loop.
- Where the action log lives; the engine's store is the natural place.
- The settings keys for strategy, tuning, modes, policies and launcher.
- How the engine speaks MCP with an outside-agent login, and whether the engine or an agent thread makes T3 tool calls.
- The stall watcher's thresholds and the grace period.
- Migration order. A suggested order: the T3 launcher behind the adapter contract, with the thread id stored in the run record and linked from the board; the engine heartbeat; stall detection and recovery; the architect channel and the issue-filing worker; the decision graph. The builder may reorder.
- Any change to the architecture documents goes through the architecture agent.
