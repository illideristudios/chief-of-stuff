---
name: optional-features
description: Use for PARA filing, requirements checklists and notifications when the Coordinator workspace enables them.
---

### Filing

Only when the workspace `CLAUDE.md` explicitly defines PARA filing homes may you file into them: `Projects/` (time-bound work), `Areas/` (ongoing), `Resources/` (reference), `Archives/` (finished). In that workspace, you may move a file or folder to its defined home without a yes. If the workspace does not define those homes, do not infer a filing system or move files under this section.

- A move is `mv`, nothing else. Never copy, delete, rename, or edit what you move. If the destination folder is missing, `mkdir -p` it first.
- Never move anything outside the workspace root unless the user has asked for it.
- Never move: the daily logs, the templates, `CLAUDE.md`, anything the `## Coordinator` block names, `Resources/skills/`, a git checkout (a folder containing `.git`), or a file whose home you cannot name with confidence. A file whose contents span more than one home — a mixed capture list, a scratch page — has no home; reasoning your way to the nearest folder is not naming it with confidence. Leave those where they are; for the unclear one, ask one question.
- Each move gets a tracker Log line with the clock time, the old path, the new path, and the reason, and is stated in your reply.
- A move breaks paths written in other files. List those in your reply; repair authorized references in the same move.

### Requirements

A deadline line in the `## Coordinator` block may name a requirements file (`- Final: <date> <time>; requirements \`<path>\``): the explicit requirements for that goal, as `- [ ]` lines under `## ` headings. The board shows each one with its done count until the deadline passes. A deadline line may also say `named tasks only` (`- PCCAT 1st attempt: <date>; named tasks only`): tasks whose `due` names it draw to it, and no task falls to it by default — an exam governs nothing on the Tasks table, and once every other deadline has passed the board's horizon is end of day, not the exam.

- The file is the user's list. Your only edit is a tick: flip `- [ ]` to `- [x]` and append ` — evidence: <commit, URL, or Log HH:MM>`, with Edit. Never add, remove, reorder, or reword an item, never untick one, and never tick without evidence.
- When a task goes `done`, read the requirements files. If the task's report is evidence for exactly one item, tick it and add a Log line `HH:MM ticked <deadline> requirement: <item>`. If it could be evidence for several, or the report gives no evidence, tick nothing and name the item in your reply. A task that matches no item ticks nothing.
- A tick is part of the move; the board shows it on its own, like any tracker edit.
- When the user asks for a goal's requirements checklist and no file exists, writing it is work from the source documents: a task dispatched under Dispatch authority, never inline. Add the `requirements` path to the block when the requested checklist is ready.

### Notify

When the block has a `Settings:` line and its `[notify]` adapter is not `off`, you tell the user about four things through `chief-of-stuff notify --root .`. Notifications are off by default, including an empty `[notify]` table. The optional `md-notify` adapter reads a markdown queue at the configured path.

- **Deadline warnings and the day's open and close:** the independent notification service owns reconciliation, including after deadline and precise `deadline:` Decisions-row edits. Never run routine `notify sync` or edit the queue yourself. The launcher ensures the service before all runtime launches; at Open the day or Resume, use `chief-of-stuff notify --root . ensure` for a direct plugin launch or a reported service failure. It is idempotent and does nothing when notifications are off. Use `chief-of-stuff notify --root . status` to report service errors. A running service checks input changes every five seconds and fully reconciles every minute while this session is closed. Manual `sync` is for explicit diagnostics only; notification events still use `add` below.
- **Awaiting you:** `chief-of-stuff notify --root . add --kind awaiting --what "<the ask, a few words>"` whenever a move ends on an ask (see Asks), parks an issue with its preparation, or adds a Decision queue row. Once per ask, in the same move as the ask.
- **A session stopped:** `add --kind stopped --what "<session>"` when the audit reports its stop file.
- **A session gone:** `add --kind gone --what "<session>"` when you set its task `orphaned`.

The script builds the title from the kind and its own clock read, so the same event never notifies twice; it prints what it added, `notify: off`, or `notify: nothing to change`. Never write the queue file with Write or Edit, and never add rows by hand: the user taps Complete, Ignore or Snooze on a banner, and the app edits the same file.
