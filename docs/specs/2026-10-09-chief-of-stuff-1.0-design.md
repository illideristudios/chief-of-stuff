# chief-of-stuff 1.0: design

Status: draft for the owner's review. Written 2026-10-09 from a brainstorming conversation, a read of the repo at origin/main v0.128.64 (PR 604 bumps it to 0.128.65), and a mining of 45 coordinator sessions (2026-09-17 to 2026-10-09, 901 user turns; counts approximate).

Notation: **Decided** is the owner's call, in the owner's words or by the owner's pick. **Call** is a choice made here without asking; the owner should check these. **Open** needs the owner.

## Intent

chief-of-stuff runs the owner's day: it turns issues into dispatched work, drives review and merge, and keeps a board of it. Today it needs constant prodding. 1.0 exists so that it is autonomous by default and the owner is consulted only on exceptional cases.

## What hurts today

From the session mining. Counts are approximate.

| Pain | Count | What happened |
|---|---|---|
| Silent or empty ticks | 127 of 901 turns follow "Mailbox empty" or "." | The owner asks for status or types "continue" |
| Ready work not pushed | ~12 | Idle worker slots with dispatchable work (approved 12 workers, ran 5) |
| Approved work not merged, reviews not launched | ~13 | Stages wait for the owner's cue; reviews launched one at a time |
| Questions it should not ask | ~90, ~10 rebukes | Relayed worker "allow?" prompts, "should I edit CLAUDE.md?", handing work to idle sessions |
| Decision pages wrong | ~30 | Answered but still pending; missing; trivial items that should have been automatic fixes |
| Lost place | ~90 | Restarts, compactions, quota hits and outages; it stopped until the owner typed "continue" |
| Stale or duplicated board | ~14, plus the owner's own report | One issue became 30-40 gantt lines |
| Acted on wrong state | ~8 | Called failing MRs mergeable; said a launch succeeded when it failed |

Pattern (inference, not verified): the coordinator leans on the model to notice what to do. Part of an engine already exists: `flow.py` `tick()` (`scripts/flow.py:281`) dispatches ready tasks and flags stalled ones, `architecture/ARCHITECTURE.md` §9 describes automatic dispatch, and the README describes a Python watcher that audits every five minutes for Codex, Cursor and Antigravity. But the mining shows ticks that said nothing about overall state, slots left idle, and approved work left unmerged, so the part that exists does not cover merge, review launch, resume after quota or outage, or the decision to ask. State is a markdown file parsed by roughly 150-170 regex calls, with older Tasks table layouts still supported (`scripts/tracker.py`), and the identity of a task is a free-text name, so nothing stops a duplicate. Each past fix was added as prompt text or memory with no gate in the engine.

## Goals

1. **Autonomous by default.** No routine nudges. The owner's four success signs: zero routine nudges (this goal), one gantt line per issue (goal 2), survives a restart with no catch-up prompting (goal 3), and merges land unprompted (goal 4).
2. **One gantt line per forge issue.** Everything done for an issue (dispatch, review, rework, merge) appears as segments on that one line. Duplicates cannot be created.
3. **Survives restarts, compaction, quota and outages** and resumes on its own.
4. **Merges land unprompted.** Work that satisfies the review policy (decision 5) merges with no human input, and the owner sees it done.

## Non-goals (Decided)

- Migrating old daily logs. Past trackers stay as archived files; the new store starts fresh and does not import the five legacy layouts.
- Multi-user or team use.
- Codex, Cursor and Antigravity support (Decided: Claude only for 1.0).
- New agent types or features. 1.0 reproduces current behaviour plus the goals above.

Not a non-goal: the board UI may be adjusted where the new data model needs it (see Board).

## Anti-goals

A 1.0 that works but still needs prodding along has failed (Decided). So has a 1.0 where the same narrow bug-fix PRs keep arriving in the same areas (the current churn is concentrated in `audit_tasks.py`, `one_shot.py`, `dispatch_prompt.py`, `git_trees.py`).

## Decisions

1. **The forge is the source of truth; the store is a rebuildable cache** (Decided, owner 2026-10-10: "the source of truth must be the forge"). Issue identity, stage/status, review and merge state live on the forge (GitHub and GitLab). The local structured store caches that plus engine-only data (stage segments, attempts, trio verdicts, limits, resume state) and can be rebuilt from the forge. Stage and status are written back to the forge: GitHub Project Status on GitHub, labels on GitLab (owner, 2026-10-11). No importer of old markdown trackers is built: at cutover the engine rebuilds in-flight work from open issues and PRs. State leaves markdown for the store (Decided). The owner never opens the tracker or daily log; only agents do. Tasks, decisions, file ownership, log and resume state live in the store. Markdown survives only where something needs a readable export. The store is accessed through an ORM (the owner's standing rule: ORMs are required).
2. **The issue is the identity** (Decided). One forge issue is one gantt line and the primary key of its work. Agents write by upsert on that key, so a second line for the same issue is impossible, not merely discouraged.
3. **No work without an issue** (Decided). If work starts with no forge issue, the coordinator creates the issue first.
4. **The engine drives; the model advises** (Decided). A long-running deterministic engine reconciles state against git and the forge on its own schedule: dispatches ready work into free slots, launches reviews, merges when policy is met, resumes after quota or outage, re-renders the board. It calls a model only for judgement (reviewing, real ambiguity). The engine, not the model, decides whether to ask the owner.
5. **Review and merge run as a declarative policy the engine enforces** (Decided). The route as the owner describes it for normal cases: burn down to no open findings, consult the architect. The model does the reviewing; the engine enforces the gates and merges when the policy is satisfied. Merging under the policy is not an interrupt. The repo today has a single `reviewer_session` or an `extras:code-review` worker plus an optional `architecture_reviewer` that receives only filed findings (`skills/review-pipeline/SKILL.md`, `architecture/ARCHITECTURE.md` §6); the policy content is set as data (decision 12), owner 2026-10-11: what counts as an "open finding" that blocks merge is set per persona and stage, with a default shipped per persona; when the architect is consulted is policy driven, not hard-coded; the retry limit is 3 fix rounds, then an unrecoverable-failure ping, adjustable per room. Reviewer roles and models and the shipped per-persona defaults are still Open. The Triage rule is settled (owner, 2026-10-11, by the owner's existing definition): clear fixes dispatch automatically; the architect disposes of each finding as fix, file, keep or discard; burn down to no open findings; ping after 3 fix rounds.

5a. **Decision review gate: a trio before any ask** (Decided). Before the system asks the owner for a decision, it must first consult a trio of reviewers appropriate to the topic. If all three are unanimous, the system adopts their recommendation. If any one has concerns, the decision goes to the owner. This is separate from code review above and is the mechanism that keeps routine decisions from reaching the owner. Not written down in the repo or the original workspace's config at the time. Settled by the owner 2026-10-11: the room/stage policy maps a decision topic (from its tags, no model call) to three personas, with a default trio per persona set; the record keeps the decision, each persona's vote and reasoning, the adopted recommendation and whether it was unanimous, on the issue and the board item; and an auto-decision can be reversed from the board, which reopens it and re-routes the work (irreversible steps still need the policy's own gates). The topic tags and the shipped default trios are Open.

6. **Exceptions that interrupt the owner** (Decided): real ambiguity in the goal (after the trio gate in 5a) that nothing in the repo, issue or earlier decisions settles; a failure the coordinator cannot recover from after its own retries; budget or quota about to exceed a limit the owner set. Everything else it decides alone. Merging under the policy is explicitly not on this list. This overturns the default `[workflow] merge_owner = "user"` and the human-only-actions section of `agents/chief-of-stuff.md` (lines 119-120, 146); the original workspace ran `merge_owner = "worker"` as a temporary waiver from 2026-10-02.
7. **Claude only for 1.0** (Decided; supersedes an earlier pick of all four hosts). Codex, Cursor and Antigravity are out of 1.0. Call: keep the core host-neutral where it costs nothing, so a second host is an adapter later, but build and test only Claude. Codex, Cursor and Antigravity adapters in the current repo (`start_coordinator.py` and the watcher) are not carried over unless examined and kept. The owner, 2026-10-10: 1.1 must support the other hosts right away, so the runtime registry (`runtimes.py`) is kept and the model port is built host-neutral from the start, not retrofitted.

7a. **The only way the owner talks to it is the board** (Decided). There is no persistent chat coordinator session. The model side is a backing coordinator session that is driven by the engine and speaks JSON only, via an API, not an interactive CLI chat. The surface is `claude -p --output-format json` (Decided), the CLI that is already installed, so no new SDK or library is needed. Every model call, including the trio gate and anything else the owner's decisions rely on, goes through a single model adapter (a port with a `claude -p` implementation behind it), so nothing else in the system spawns `claude` or parses its JSON (Decided).

8. **Board** (Decided): adjust it where the model needs it; no redesign. Call: rework and review show as segments on the issue's single line.
9. **Workers launch under a small guard plugin** (Decided, 2026-10-10). The engine starts each worker with a purpose-built plugin of Python command hooks, not the user's full plugin set: gates on forge writes and merges outside the policy, `AskUserQuestion` and permission relays routed to the engine (and from there to the trio gate), and a Stop record gate that stops a worker finishing before it has reported to the engine. Hooks fail open (a hook that errors or times out is skipped), so they are guard rails, not the authority: the engine independently verifies state before every merge. Judgement calls through the model adapter use `--safe-mode`, which disables hooks, plugins and CLAUDE.md.
10. **Typed report/ask channel** (Decided, 2026-10-10). The engine owns a Python MCP server that gives workers `report` and `ask` tools, replacing the file mailbox and TOON files. Reports and asks land in the store through the same adapters as everything else.
11. **Session-start state injection** (Decided, 2026-10-10). Each worker starts with its issue's state injected by a session-start hook, so a restart or resume needs no catch-up prompt.
12. **Policy is data, by persona, stage and room** (Decided, owner 2026-10-10: "everything is policy driven. We have default policies, and based on personas. This is all configurable at a stage / room level"). A persona is a reviewer/actor profile (role, model, effort, rules). A room is both a board section and a persona's space. Default policies ship per persona, and any gate, limit or review rule can be overridden per stage and per room. Gates (design, plan, triage, merge) are therefore configuration, not code. Standing approvals are policy exceptions and must carry an expiry timer; one with no expiry is refused. Making an exception permanent by building it into the loop through a mod is deferred. An agent is a more advanced persona: a full Claude Code agent (for example Frank Lloyd Aight) that has evolved past a single profile and may use personas itself; a workflow stage can name either.
13. **Workflows are directed graphs** (Decided, owner 2026-10-10: "make it so we can create directed graph workflows for different types of things. we'll ship with only one to start"). A workflow is a directed graph of stages with gate policy on the edges, read from data. The issue type selects the workflow. 1.0 ships one default workflow. This replaces the `[lanes]` settings and the `workflow` token. Review and rework are segments on the issue's one line (decision 8), so every unit of work has an issue.

## Build approach (Decided)

Mostly clean-slate ("3 ish"). The old specs, old golden evals, old Python code and old tests are references, and each item brought across is examined critically first rather than ported on trust. Nothing is carried over by default. The Clean Architecture contract (`architecture/ARCHITECTURE.md`, enforced by `evals/test_architecture.py`) is the target: new modules sit in circles, and closing the open compliance rows C1-C8 is part of 1.0.

## Constraints

- Any new library, package or external tool needs the owner's approval before it is installed, vendored or depended on: the ORM, the store, anything else. Name it, say where it comes from and why, and wait for the owner's yes. Researching candidates is fine.
- The existing test suite is the main safety net (about 49k lines of tests, 110 eval cases). Whatever the build approach, the behaviour it encodes must be carried over or consciously dropped.
- The plugin repo protects `main`; every change lands through a PR and bumps the version.

A mod (TypeScript function hooks, in-terminal status line, native notify) is allowed: the repo rule is "Python (or another real language)", so TypeScript needs no exemption. Whether the guard plugin in decisions 9-11 is Python command hooks plus an MCP server or a TypeScript mod is Open. The in-terminal UI part of a mod is deferred on merit (the board is the interface), not on language.

## Left to the builder

- The concrete store, schema and ORM (candidates to be presented to the owner for approval).
- The engine's process model: how it is started, supervised and restarted, and how it talks to each host's session.
- How the model-facing prompt shrinks once the engine owns the loop.
- The policy file format for the review route.
- Which current behaviours (notifications, quota relaunch, worktree hygiene, resource cleanup) carry over unchanged. Stale worktrees used 185 GB once, so cleanup is a behaviour to preserve.

## Open

- **The code-review policy's content**: reviewer roles and models, what counts as an "open finding", retry limits, when the architect is consulted. (The Triage rule is settled; see decision 5.)
- **Which current behaviours carry over versus are dropped**: tick, kanban, notify, PARA filing, the herdr/tmux/ghostty launchers, `claude_profile`, the mailbox, decision pages. Decided: decisions that pass the trio gate become items on the board, where the owner answers them (no separate decision pages), and notify sends exception pings only, linking to the board item.
- **Budget and quota limits**: Decided: set in the engine's own config (spend, quota and time limits), which a workspace can override. The default values are measured through the 1.0 lifecycle and set in 1.1 (owner, 2026-10-10).
- **Gantt segments**: what a segment is keyed on when rework yields several PRs or workers for one issue. Call: key segments by (issue, stage, attempt).
- **Forge coverage for "create the issue first"**: GitHub and GitLab, and workspaces with no `Backlog:` line. Decided: a workspace with no `Backlog:` line is not run. The engine refuses the work and instead helps the owner set up or configure the backlog. Setup help is part of 1.0.

- **Form of the worker guard plugin (decisions 9-11)**: the owner, 2026-10-10: "leave it all in the design for later." Candidates: a TypeScript mod (gates that can fail closed through `.catch`, plus `$.tool.register` for `report`/`ask`, plus session-start injection), Python command hooks plus a Python MCP server (gates fail open), or a mod for the gates with the MCP server for the channel. Unverified: that a `claude -p` worker loaded with `--plugin-dir` runs function hooks and honours `.catch`. Decide after a spike.
- **How the engine supervises and restarts** the `claude -p` calls and carries session state between them (see 7a).
- **Choosing the trio**: how the topic-appropriate three reviewers are picked, and what record of their verdict is kept.
