# Project: chief-of-stuff

A versioned coordinator for Claude Code, Codex CLI, Cursor CLI, and Antigravity CLI. The Claude plugin ships `agents/chief-of-stuff.md`; `start_coordinator.py` adapts the same rules for the other hosts.

## Rules

- **TDD.** Behaviour starts as a failing eval case. The agent file changes only to turn a red case green.
- **Evals are runtime selectable.** `python3 evals/run.py --runtime claude|codex|cursor|agy --arm baseline|agent --model <host-model> [--case <glob>] [--runs N] [--golden]`. Claude is the default runtime and defaults to Sonnet for iteration; other runtimes require an explicit model. Run the Python suite with `python3 -m unittest discover -s evals -p 'test_*.py'`. The harness uses temporary fixtures and mocks external integrations; it records denied calls as attempts.
- **Claude plugin model policy.** The Claude agent frontmatter pins `model: opus`; Claude golden cases use `--golden --model opus --runs 3`. This policy applies to Claude runs only. Codex, Cursor, and Antigravity use their selected model for the same case definitions. A case is golden when its `case.json` says `"golden": true` with a `golden_why`; an empty golden selection exits nonzero.
- **No shell scripts.** Harness code, shims, mocks, and probes are Python (or another real language). Shell appears only as a one-line command someone types.
- **Fixtures are templates.** `{{today}}`, `{{yesterday}}`, `{{tomorrow}}`, `{{tz}}`, `{{now_hhmm}}`, `{{now±Nh}}` (`|local`, `|hhmm`), `{{dotgit}}` (a `.git` dir name, since git cannot track one), and `{{live_pid}}` (a pid alive for the whole run) in file names and contents. No hardcoded dates. An unknown token fails the run.
- **Fixtures are generic.** No data from any workspace that uses this agent — no patient data, no program documents, no real deploy names.
- **Only the architecture agent changes the architecture docs.** `architecture/ARCHITECTURE.md` and `architecture/compliance.md` are changed by the architecture agent (`frank-lloyd-aight`) and by no other agent. That includes placing a new script in §4, recording or closing a gap, and rewriting a section to match code. Every other agent reads them, leaves them out of its changes, and reports to the architecture agent where code and document disagree. The architecture agent changes no code.

## Architecture

Read by `claude --agent frank-lloyd-aight`. Every path, name, and tool the architecture session uses comes from this block.

- User: Zach, he/him
- Project: chief-of-stuff
- Architecture directory: `architecture/`
- Canonical document: `architecture/ARCHITECTURE.md`. Kept canonical by this session, never moved, never renumbered.
- Plan directory: `~/.claude/plans/`
- Review directory: `architecture/`; pages are named `<subject>-review.html`
- Publisher: self-hosted; URL http://127.0.0.1:8787/; dir `/Users/locriani/Developer/Gauntlet/Areas/pages/`. A review page written there is at `http://127.0.0.1:8787/<subject>-review.html`.
- Session tools: `ListAgents` to find sessions, `SendMessage` to reach one
- Diagram renderer: `~/Developer/ai-additions/plugins/mermaid-system-design/skills/mermaid-system-design/scripts/mermaid-check.py` (exit 0 rendered, 1 a diagram failed, 2 renderer missing). A diagram that did not render is never approved.
- Commits: this session commits its own work in its own worktree, in small meaningful commits, and pushes each one. It does not merge.
