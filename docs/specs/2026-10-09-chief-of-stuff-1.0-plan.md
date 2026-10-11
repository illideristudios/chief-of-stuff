# chief-of-stuff 1.0: implementation plan

Plan for [`2026-10-09-chief-of-stuff-1.0-design.md`](2026-10-09-chief-of-stuff-1.0-design.md), which Zach approved on 2026-10-09. Decision numbers (D1, D5a, …) refer to that document's Decisions section. Nothing here is built yet.

## Ground rules

From the repo's `CLAUDE.md` and the design's Constraints. They bind every milestone.

- **TDD.** Behaviour starts as a failing eval or unit case; code changes only to turn it green.
- **No shell scripts.** Everything is Python.
- **Fixtures are templates and generic.** No workspace data.
- **Clean Architecture is the target** (D: architecture answer): every new module sits in a circle, `evals/test_architecture.py` enforces it, and rows C1-C8 in `architecture/compliance.md` are closed as part of 1.0.
- **Every model call goes through one model adapter.** The trio gate, the review verdicts, the backing coordinator session, and anything else that talks to Claude via `claude -p --output-format json` use a single model port with a `claude -p` adapter behind it. No module outside that adapter spawns `claude` or parses its JSON. The same rule is how a second host stays an adapter later (design D7).
- **Examine before importing.** Old specs, golden evals, code and tests are references. Each item brought across gets an entry in a carry/drop ledger (M0) with a reason.
- **No new dependency without Zach's yes.** Name it, say where it comes from and why, wait. The ORM and store are the first such request.
- **Every change lands through a PR on a branch and bumps the plugin version.** `main` and `develop` are both protected. Work targets `develop`; `main` takes `develop` at release (Zach, 2026-10-10).
- **Host-neutral from the start.** 1.1 must support Codex, Cursor and Antigravity right away, so the model port and the runtime registry (`runtimes.py`, kept) are built host-neutral now. 1.0 builds and tests only `claude`.
- **The forge is the source of truth; the store is a rebuildable cache** (design decision 1).

## Milestones

Each milestone ends with exit criteria that are runnable checks. The four success signs from the design are the acceptance test for 1.0: **S1** zero routine nudges, **S2** one gantt line per issue, **S3** merges land unprompted, **S4** survives a restart with no catch-up prompting.

### M0. Approvals, spikes, and the carry/drop ledger

No product code. Produces decisions.

1. **Store and ORM proposal for Zach's approval.** Present candidates with source and reason (a Python ORM over SQLite is the likely shape; candidates and trade-offs to be researched, none adopted). Block M1 on his yes.
2. **`claude -p --output-format json` spike.** This also fixes the shape of the model port the adapter implements (request, structured response, session handle, error/quota result). Prove: structured judgement calls (a review verdict, a trio vote), session continuity across calls, behaviour at quota or outage, cost accounting. Output: a short note and a throwaway script in `_scratch`, not shipped.
   Add to this spike (deferred by Zach, 2026-10-10, to run later): does a `claude -p` worker loaded with `--plugin-dir` run TypeScript function hooks, and does a `.catch` make a gate fail closed? The answer picks the guard plugin's form (design Open items).
3. **Carry/drop ledger.** One row per current behaviour or module (tick, kanban, notify, PARA filing, launchers, `claude_profile`, mailbox, decision pages, the 110 eval cases), each marked carry, rewrite, or drop, with the reason. Zach reviews the ledger once.
4. **Behaviour spec extraction.** From the golden evals and the architecture doc, list the behaviours 1.0 must reproduce. Resolve the design's remaining Open items that block M1-M3: policy content for code review, trio selection rule, default limits.

Exit: store approved, spike note, ledger approved, open items resolved or deferred explicitly.

**M0 status (2026-10-10):**
- Store and ORM: **approved** by Zach: SQLAlchemy pinned to the 2.0.x line (2.0.54) with Alembic 1.20.0 on SQLite via stdlib `sqlite3` (adds `mako`, `markupsafe`, `typing-extensions`). Nothing is installed until M1 starts. Runner-up was Peewee.
- Spike: done; see [`2026-10-09-chief-of-stuff-1.0-spike-claude-p.md`](2026-10-09-chief-of-stuff-1.0-spike-claude-p.md). Quota and outage behaviour is unverified and must be treated as retryable-unknown.
- Ledger: **reviewed with Zach row by row on 2026-10-10**; his decisions are in its Owner decisions section. Open: the Triage rule (M4) and why requirements checklists broke; see [`2026-10-09-chief-of-stuff-1.0-ledger.md`](2026-10-09-chief-of-stuff-1.0-ledger.md).
- Ledger carry-ins that shape the milestones below: policy by persona, stage and room (decision 12); directed-graph workflows with one shipped (decision 13); the engine removes worktrees under the finding-27 guards (E19); launchd supervisor (E18); requirements checklists carried; the performance report kept as a store query; the install-and-pin mechanism dropped; the runtime registry kept; non-Claude hosts in 1.1.
- Settled 2026-10-11: workers go through the adapter by default with a full session when needed; open-finding severity is set per persona and stage; the architect is consulted by policy; 3 fix rounds then a ping.
- Still open: the shipped per-persona default policies (reviewer roles and models), the trio selection rule, why requirements checklists broke (find the cause before porting), and the datetime-inject version range. The clock hook is a plugin dependency on `datetime-inject` (Zach, 2026-10-10), not a hook shipped here. The two spikes are done (see the spike note addendum): command hooks run in `claude -p` workers, hooks fail open, and plugin dependencies install automatically but only within one marketplace unless allowed. Default limit values are no longer a 1.0 item: they are set from 1.0 measurement in 1.1.

### M1. Core model and store

1. Schema for work items keyed by forge issue, stage segments keyed (issue, stage, attempt), decisions, file ownership, log, resume state. Unique constraint on the issue key.
2. Repository layer behind the ORM, upsert-only writes.
3. Tests first: writing a second row for an existing issue updates it, never inserts; concurrent writers cannot create a duplicate; resume state round-trips; **the store can be dropped and rebuilt from the forge** (issue identity, stage, review and merge state come back, engine-only data is recomputed or marked lost).

Exit (S2 at the data layer): a test that tries to create a duplicate gantt row for one issue and cannot.

### M2. Forge adapter and the issue-first gate

1. GitHub and GitLab adapter: read issues and MRs/PRs, create an issue, read CI and review state, **write stage/status back to the forge** (labels or Project Status; choice Open), and rebuild the store from the forge.
2. Issue-first gate: starting work with no issue creates it first. A workspace with no `Backlog:` line is refused, and the setup helper walks Zach through configuring one (D3, Decided).
3. Forge calls prefer `gh` subcommands over `gh api` where they cover the call.

Exit: eval cases for work-without-issue (issue created, then one row) and no-backlog (refused, setup offered).

### M3. The engine loop

1. State-driven reconcile (replaces mailbox-driven ticks): each pass computes what the state calls for. Free worker slots with dispatchable work → dispatch. Approved, policy-satisfied work → merge. Missing review → launch it. Reviews run in parallel.
2. Model adapter: the `claude -p --output-format json` implementation of the model port, with fakes for tests so engine evals never call a real model. Workers launched in git worktrees. Workers run through the adapter by default, and as a full Claude Code session only where a persona or stage says so (Zach, 2026-10-11); the launch profile in item 3 applies to both.
3. Worker launch profile (D9-D11): a guard plugin of Python command hooks (forge-write and merge gates, ask and permission relay to the engine, Stop record gate, session-start state injection) and the engine's Python MCP server with `report` and `ask` tools. Tests first: each gate has a red case that a worker cannot get past, and the engine's own merge-time check passes when a hook is skipped. Replaces the mailbox and TOON files (ledger: riskiest drop 1).
4. Supervisor: runs as a launchd LaunchAgent (E18, reusing the install code in `notify_service.py`), restarts itself and restarts on plugin update (the install-and-pin mechanism is dropped, so this covers instruction/script skew, finding 60), and resumes after quota reset or outage by waiting, not by asking.
5. Cleanup: **built in the engine** (the repo has no cleanup code today; stale worktrees once used 185 GB). It removes finished worktrees and build output only past the finding-27 guards: owner check, `git status --ignored`, bound containers, unlanded-work check. The model and workers still never remove trees (B10).
6. Carried modules ported with their tests: git view hardening (F1), unlanded-work rule (F3), worktree creation (F4), merge-readiness verdict (F9), process liveness (E8), login-shell resolution (E14), concurrency cap (E21).
7. **Runtime registry (E13) and the host-neutral port.** `runtimes.py` is ported as the host table behind the model port, with the `claude_as_profile` launch (E12) applied through it. Tests first: the `claude` entry builds the right argv (effort flag, profile launch, missing `claude-as` reported, never a silent fallback to bare `claude`); the engine and adapter reach a host only through the registry and the port (an `evals/test_architecture.py` rule that nothing outside the adapter names a binary); the `codex`, `cursor` and `agy` entries load as data and are checked for shape only, with their adapters built in 1.1.
8. **Clock** (B4, carried): the engine reads time from one Python clock module, and workers and the backing session get the clock from the `datetime-inject` plugin, a declared dependency of chief-of-stuff that the worker launch profile (item 3) loads, not from the model. Tests first: the engine's deadline and age logic runs against a pinned clock; a worker launched with the profile receives one bracketed timestamp line per prompt (whether `claude -p` workers run the hook is the open M0 spike).

Exit (S1, S4): an eval that kills the engine mid-run and a fake quota hit, and the run resumes with no prompt and no state loss. Also: the registry and clock tests in items 7 and 8 pass.

### M4. Review and merge policy

1. Policy and workflow as data (decisions 12 and 13): personas (role, model, effort, rules), rooms (a board section and a persona's space), stages, and one shipped directed-graph workflow with gate policy on its edges. Default policies ship per persona and are overridable per stage and room. Reviewer roles and models, what counts as an open finding, retry limits and when the architect is consulted live here. Content settled in M0. Standing approvals are policy exceptions that must carry an expiry; one without is refused.
2. Engine enforces the gates (burn down to no open findings, architect consult) and merges when the policy is satisfied. Merging under policy is not an interrupt; this overturns the `merge_owner = "user"` default (D6).
3. **Revisit the Triage rule in this phase** (Zach, 2026-10-09: "make a note to visit this during this phase"; no direction chosen yet). Today it asks about any non-trivial or security finding (`skills/review-pipeline/SKILL.md`, Triage), which conflicts with consulting Zach only on exceptions. Options raised: route every would-be ask through the trio gate; the same except security and Critical findings, which always go to Zach; or widen the automatic-fix rule and trio the rest. Decide with Zach during M4, before the rule is carried.

Exit (S3): an eval where work meeting the policy merges with no human input, and one where a policy gate fails and it does not. Also: a gate turned on for one stage or room holds only there, and an exception with no expiry is refused.

### M5. Decision gate and interrupts

Zach, 2026-10-09: "we'll revisit at m5 time." This scope is provisional; reopen it before starting M5.

1. Trio gate (D5a): before any ask, consult a topic-appropriate trio through the model adapter (three calls, structured verdicts). Unanimous → adopt their recommendation and record it. Any concern → escalate to Zach.
2. Trio selection rule and verdict record (settled in M0).
3. Interrupt set: real goal ambiguity after the trio, unrecoverable failure after retries, budget or quota about to exceed a limit.
4. Engine config holds spend, quota and time limits, with workspace override. Ships with no hard default values: the engine records spend and time per issue through the 1.0 lifecycle, and the defaults are set from that measurement in 1.1 (Zach, 2026-10-10).

Exit (S1): evals showing a routine question resolved by a unanimous trio with no ping, a split trio escalating, and a limit breach pinging.

### M6. Board and pings

Zach, 2026-10-09: revisit at M6 time. This scope is provisional; reopen it before starting M6.

1. Board reads from the store. Gantt shows one line per issue with segments by (issue, stage, attempt). Adjust the existing UI only where the model needs it (D8).
2. Decisions that pass the gate are board items, answered on the board. No separate decision pages.
3. Notify sends exception pings only, linking to the board item.
4. Agent performance report as a store query over stage segments (A3, kept).
5. Requirements checklists (H10/V24, carried): done tasks tick one item with evidence, and the board shows done counts. The feature is: Zach feeds in a PRD, the engine distils a checklist from [a template](2026-10-10-requirements-checklist-template.md), and done tasks tick items with evidence (Zach, 2026-10-10: "we need a template requirements checklist"). M0 still finds why the old feature broke; the known suspects are the board parser silently skipping non-canonical checkbox lines, the syntax moving into an optional skill (#510), and ticking depending on a model reading that skill. Nothing is ported until that cause is written down. Distilling, matching a done task to an item, and ticking stay model judgement, made in collaboration with Zach; the engine only parses and counts (Zach, 2026-10-10).

Exit (S2): a board render test over a day with rework on one issue shows exactly one line with several segments.

### M7. Cutover and 1.0.0

1. Run the new engine beside the old harness on one workspace; compare against the old behaviour using the golden cases.
2. Fix gaps found; retire the old harness paths that are not carried (markdown tracker, mailbox and TOON, terminal launchers, install-and-pin, PARA, notify service, share redaction). Update the repo `CLAUDE.md` for the Claude-first engine; its rules move into per-space workflows and rooms as they land.
3. Cut `1.0.0` (plugin.json and marketplace.json) by merging `develop` into `main`.
4. Bake: a week of real use judged against S1-S4.

Exit: S1-S4 hold for a week of real use.

## Risks

- **The model surface is a CLI, not a server.** `claude -p` per call means session continuity and cost accounting are the engine's job. The M0 spike exists to find the limits early.
- **Evals are large.** The old suite is ~49k lines. Porting by hand is slow; the ledger makes the carried set explicit so it stays small.
- **Policy content is the unsettled core of M4 and M5.** If M0 does not settle it, those milestones stall; budget a decision session with Zach inside M0.
- **Overturning `merge_owner = "user"`** removes a human gate. Mitigation: the policy must be demonstrably satisfied and logged, and the limit and exception paths keep a human in the loop for anything exceptional.

## Order and parallelism

M0 first and alone. M1 → M2 → M3 in sequence (each depends on the previous layer). M4 and M5 can overlap once M3's loop exists. M6 can start after M1 against fixtures. M7 last.

## Open items carried into the plan

Resolved in M0: policy content, trio selection rule, the carry/drop ledger (done), the requirements-checklist cause, and whether workers run via `claude -p`. Resolved in M3: supervision and session-state handling across `claude -p` calls.

## Deferred to 1.1

Zach, 2026-10-10: 1.1 supports the other hosts right away.

- Codex, Cursor and Antigravity: their model-port adapters, prompts and watcher (G5, E16's non-Claude parts), runtime prefixes and `--not-family` in model rotation (E20), and the three non-Claude eval backends (I2).
- Default values for spend, quota and time limits, set from what 1.0 measures.
- Interactive dispatch proposals and the peer-session eval family (V8, V20).
- Calendar rules (B5) and `mock_calendar`. The clock comes from a datetime-inject-style hook in 1.0.
- Making a policy exception permanent by building it into the loop through a mod.
