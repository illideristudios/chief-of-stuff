# chief-of-stuff

A daily coordinator for Claude Code, Codex CLI, Cursor CLI, and Antigravity CLI. The workspace keeps its existing `CLAUDE.md` `## Coordinator` block. The coordinator routes work, keeps the tracker and board current, and automatically dispatches ready one-shot tasks and asks before new interactive sessions. Every decision it puts to the user links a page with the full context: the question, background, options, a recommendation and the default. With a self-hosted board, `chief-of-stuff decision` renders that page into the pages dir and prints its local URL. Without one, the page is a comment on the issue or pull request the decision is about. A reply asks one decision at a time.

## Install and launch

Install one command from this checkout:

```sh
python3 chief_of_stuff.py install --bin-dir /usr/local/bin
```

This writes a stable launcher in `/usr/local/bin` and a versioned release under `~/.local/share/chief-of-stuff/`. The system directory needs write permission only when first installing or explicitly replacing a different launcher; later installs update the user-owned `current` pointer. Use `--dry-run` to preview the paths without writing. The command routes a fixed set of operations: `start`, `init`, `models`, `worktree`, `worker`, `audit`, `processes`, `result`, `board`, `inbox`, `backlog`, `kanban`, `pipeline`, `notify`, `health`, `pages`, `decision`, `log`, `merge-approved`, `merge-ready`, `review-threads`, `sources`, `tracker`, `limits`, and `tick`. Use `chief-of-stuff <command> --help` for each command's options.

For a new workspace, create a minimal `## Coordinator` block and settings file, then launch a coordinator:

```sh
chief-of-stuff init --root /path/to/workspace --user "Your Name" --timezone America/Chicago --github-repo owner/repo
chief-of-stuff start --runtime codex --root /path/to/workspace
# also supported: --runtime claude, cursor, or agy
```

The initializer appends to an existing `CLAUDE.md` when it has no Coordinator block, refuses to replace one that already exists, and leaves existing settings intact. It copies an editable provider and model guidance template to `chief-of-stuff-models.md` in the workspace root without replacing an existing copy. For an already configured workspace, run `chief-of-stuff models --root /path/to/workspace` to add only that file. `--dry-run` previews the addition. Choose a forge: init requires `--github-repo owner/repo` or `--gitlab-host https://HOST` with `--gitlab-project group/repo`. Optional flags include `--agent implementer:task`, `--launcher tmux`, `--worker-mode one-shot`, and `--board-port 8765`. The board port configures serving. No coordinator or worker may automatically open the board URL or rendered HTML. Render and serve only; the user opens it. Claude launches load a pinned plugin hook that blocks board-opening shell and browser tool calls while allowing render, serve and HTTP checks. Notifications start off. Add calendar and deadline lines when those integrations are available.

`chief-of-stuff health --config /path/to/workspace/CLAUDE.md` names a missing or non-file settings TOML and shows the init command. That line is advice and leaves the exit code unchanged; a missing or non-file config path is a usage fault (stderr, exit 2); settings, runtime and service failures still count as failed checks. Health reads init's file inventory and respects custom config and settings paths.

`chief-of-stuff start` resolves the selected CLI through the configured interactive login shell (`$SHELL`, or the account login shell) so shell startup paths are honored, and refuses a missing runtime binary. It pins rules and scripts to one release for the life of that coordinator. `--dry-run` prints the exact launch arguments without opening a session. `python3 start_coordinator.py` from the checkout remains available as a source launch.

Claude can still use the existing plugin workflow:

```sh
claude plugin marketplace add /path/to/chief-of-stuff
claude plugin install chief-of-stuff@chief-of-stuff
claude --agent chief-of-stuff
```

The launcher starts even when the selected CLI does not expose the configured `Calendar tool:`. The coordinator reports the missing tool, leaves calendar facts unverified, and continues work that does not depend on the calendar. To enable calendar reads, configure an equivalent calendar tool in that CLI.

If a workspace requires workers to use only the configured `User:` name, add `- Identity: user-name-only` to its `## Coordinator` block. Without that line, assignments make no claim about aliases or names found in workspace files.

The coordinator's PARA filing rule applies only when the workspace `CLAUDE.md` explicitly defines the `Projects/`, `Areas/`, `Resources/`, and `Archives/` homes. Other workspaces do not gain those folders or automatic filing moves.

Notifications are off unless the workspace settings explicitly set `[notify] adapter = "md-notify"`. The default queue path for that adapter is `notifications/NOTIFICATIONS.md` under the workspace; set `queue` to use another path.

Enabled notifications have an independent macOS user service, ensured before coordinator launches on every runtime. It reconciles deadline and day notifications when configuration or tracker inputs change (checked every five seconds), every minute, and after restart, even with the coordinator closed. Use `chief-of-stuff notify --root . status` for health, `stop` to unload it, and `ensure` to restore it. Manual `sync` remains available for diagnostics. Queue writes use a shared lock; concurrent acknowledgment preservation requires the companion md-notify transaction update and a rebuilt app. See [notification lifecycle and compatibility](docs/notify.md).

For a GitHub `Backlog:` line, the pinned `chief-of-stuff backlog --create <title> --body <text> --commit` files issues through `gh` without a personal plugin. It supports native `--parent` and `--blocked-by` relationships. Writes preview by default; a workspace can still set its own issue content rules. See [backlog setup](docs/backlog.md) for GitHub and GitLab configuration.

An optional `[workflow] architecture_reviewer = "<session-name>"` in the workspace TOML routes filed architecture findings to that session. Without it, no particular person or agent is assumed.

An optional `[workflow] reviewer_session = "<session-name>"` routes pull requests to a named reviewer. Without it, the coordinator asks the user how to review a pull request instead of inventing a reviewer session.

Every reviewer reports in one shape, [`skills/review-pipeline/review-report.md`](skills/review-pipeline/review-report.md), whatever its runtime, because Triage reads each finding's axis, severity, confidence and suggestion off it. Every worker assignment points a reviewing worker at it.

`[workflow] delivery = "pull-request"` and `merge_owner = "user"` preserve the existing delivery path by default. Set `delivery = "branch"` for branch handoff without an automatic PR step, or `merge_owner = "worker"` to let the owning worker merge a reviewed PR after the coordinator confirms required approvals. Branch delivery still needs integration into main and a suite run there before a code task is marked done.

## Runtime limits

`chief-of-stuff limits --root /path/to/workspace` reads and records account limits without starting a model session or making a network call. Each engine pass records one reading; a failed reading does not stop the pass.

Claude uses structured `rate_limit_event`/`rate_limit_info` or system `rate_limit`/`rateLimitInfo` transcript records, including model caps when present. Native assistant `rate_limit` errors also establish 100% usage when they explicitly report a cap hit and a reset clock with a timezone. Usage before that hit remains unknown unless a numeric event is available. It reads the workspace's `[workers] claude_as_profile` under `$CLAUDE_AS_PROFILES_DIR` (default `~/.claude-profiles`), or `$CLAUDE_CONFIG_DIR` (default `~/.claude`) for the default login. Transcript token counts are not account limits. Missing, malformed, or expired events show unknown with a reason. Codex, Cursor, and Antigravity show unknown with the reason `read in 1.1`.

`[limits] warn_at` (default `0.8`, a fraction from `0` to `1`) configures the near-cap judgement; dispatch enforcement and board display are planned for M5 and M6. History is shared across workspaces at `$XDG_STATE_HOME/chief-of-stuff/limits.sqlite` (default `~/.local/state/chief-of-stuff/limits.sqlite`) and queries always select a runtime and account. Repeated source events and consecutive identical unknown statuses do not create fresh observations. Burn rates compare observations within one reset window. `LimitsHistory.readings(runtime, account, since)` exposes history for future dispatch-runtime queries.

## Workers

Set the workspace worker mode in TOML:

```toml
[workers]
mode = "one-shot" # or "interactive" (the default)
launcher = "ghostty" # ghostty, tmux, or herdr for interactive workers
herdr_session = "chief-of-stuff" # herdr only: the dedicated session workers run in
claude_as_profile = "secondary" # optional: Claude workers run as `claude-as run secondary`
require_green_tests = true # optional: hold one-shot and engine completions with unknown or non-green suite counts
rules = ["Never skip or weaken tests."] # optional: standing rules for every one-shot and engine worker
```

`[workers] rules` is an ordered list of non-blank, single-line strings without control or Unicode format characters or `(via ` markers, each at most 2400 characters; surrounding spaces are preserved. There are at most 64 rules and 6000 characters in all. The engine also checks each configured stage's complete assignment budget at startup, including maximum row fields, persona rules, skills, architecture and suite instructions. One-shot assignments check the complete task against the assignment cap before launch. Every one-shot and engine assignment puts these rules in its header, before the task. Interactive launches do not carry them. Omitting `rules` or setting it to `[]` leaves the assignment unchanged.

With `claude_as_profile` set, every Claude-runtime worker, interactive or one-shot, launches through `claude-as run <profile>` instead of a bare `claude`, so it runs under that account profile. `claude-as` must be on the login shell's PATH, or the launch is refused. Codex, Cursor and Antigravity workers are unaffected.

One-shot results include `passed`, `failed`, `skipped`, and `xfailed` fields from the final suite run, as non-negative integers or `null` when unknown. Workers name the suite command in `changes`; earlier failing TDD runs do not contribute to these counts. Reports retain the counts and explicitly name missing or invalid ones. Set `[workers] require_green_tests = true` to turn `done` into `human_review` unless all counts are known, `passed > 0`, and `failed`, `skipped`, and `xfailed` are zero. The engine applies that same gate when it collects a worker report: pass the four counts to `report`, and a non-green or unknown suite holds the issue for human review. The engine retries a failed hold-label write without relaunching work. Removing the published hold accepts that completion and lets the workflow continue. The setting defaults to false, so older results without counts still complete in workspaces that have not opted in. Tasks that run no suite report unknown counts and need human review in an opted-in workspace.

Set `[workers] teardown = "python3 tools/stack.py down"` to clean up each engine worker's local containers, volumes and VMs. The command runs in the workspace root through the configured interactive login shell, so a relative script is the workspace's copy. `CHIEF_OF_STUFF_WORKER` is the worker's name, `CHIEF_OF_STUFF_WORKSPACE` is the workspace root, and `CHIEF_OF_STUFF_WORKTREE` is the worker's worktree. The engine runs teardown when collecting an exited or lost worker, and after cancelling a worker whose start could not be verified. Commands have a separate 120-second limit. A cleanup failure appears in the engine's issue log; a successful worker keeps its `done` outcome. Until engine cutover, one-shot workers rely on the assignment's existing instruction to tear down their stack before finishing; one-shot launchers and adoption do not enforce cleanup.

The optional `[workers] teardown_list` setting is accepted and validated for the planned cleanup guard; it has no consumer yet. With both settings absent, no stack commands run.

With `launcher = "herdr"`, each interactive worker starts in its own tab of a dedicated herdr session (default `chief-of-stuff`, session name rule `[a-z0-9][a-z0-9_-]{0,31}`), which must already be running with an active workspace. After creating the tab, the launcher uses its returned root pane id with `herdr --session <session> pane run <pane-id> <command words>` to run the worker wrapper through the pane's shell, quoting each command word with `shlex.quote`. The typed line omits the `env -C <root> PATH=... CHIEF_OF_STUFF_WORKSPACE=...` prefix tmux and Ghostty use: the tab is created with `--cwd`, the pane shell already has the user's PATH, and the workspace reaches the worker through `tab create --env CHIEF_OF_STUFF_WORKSPACE=<root>`. A typed line over 900 bytes would be cut by the pane's tty, so the launcher refuses it, before creating a tab, instead of reporting a launch that did not happen. The launcher's own herdr calls carry no `HERDR_*` variables; herdr sets its own variables inside each pane, and isolation comes from the dedicated session's own socket. Any launch failure after a valid tab id is received attempts to close that tab. Because herdr has no `session create`, the launcher first asks the configured session's roster (`herdr --session <session> session list --json`); when a readable roster shows the session is not running, the launch is refused — naming `[workers] herdr_session` and the one-time `herdr --session <session>` that begins the session — before any tab is created. A roster request that fails or cannot be read is never treated as "not running"; `tab create` reports it. After `pane run` submits the line, the launcher waits up to 30 seconds for the worker's own registration to appear (through herdr the Claude wrapper registers too, into `<workspace>/.chief-of-stuff/sessions/<name>.json`) and treats a start that never registered as a failed start: it reports the failure, closes the created tab, and leaves the worktree reusable — a cut line or a shell that never ran the command is no longer reported as a start, and a registration left by an earlier worker of the same name does not vouch for the new one.

The settings' `[repos]` table maps a repository name to a path relative to the workspace root (`alder = "repo"`). `chief-of-stuff worktree --clone <name>` cuts from that entry; a `--clone` that is no entry's name is a path, as before. The coordinator passes the name when the table names the repository, otherwise the path. Settings that cannot be read are a refusal.

`chief-of-stuff worker --check --root . --task <name>` checks the Tasks and File ownership rows only: it needs no `--cwd`, makes no worktree, starts nothing and prints the assignment or `refused: <why>`. Add `--name <session>` for a standing row and `--one-shot` for a one-shot task; the other launch flags are ignored. After the user approves an interactive dispatch, `chief-of-stuff worker` starts a separate worktree terminal with `--runtime claude|agy|codex|cursor`. Claude remains the default. Ghostty tabs are the default launcher. Set `[workers] launcher = "tmux"` in the workspace TOML named by the `Settings:` line, or pass `--launcher tmux`, to open a new window in an existing tmux session. A launch refuses a missing tmux executable or tmux server. `--launcher ghostty` overrides the setting for one dispatch. The assignment is written to the worktree before the terminal opens; each worker reads it through a fixed bootstrap prompt. Claude uses native session messaging. Other workers register and report through the workspace file mailbox, using their assigned name and worktree. A launcher wrapper loads the configured interactive login shell for every worker and records registered worker processes under `.chief-of-stuff/sessions/` (through herdr the Claude wrapper registers too, for the launcher's start verification). `chief-of-stuff processes --root /path/to/workspace` checks all registered workers in one batch and lists each live one-shot run (its launcher PID, its task when today's or yesterday's tracker holds it, and its tree's directory name) as rows after the workers in the same list, marked `kind: one-shot`, and prints TOON. Pass space- or comma-separated PIDs to check a specific batch, including unregistered processes. `chief-of-stuff result --root /path/to/workspace` prints a finished one-shot's report by `--task`, or without it lists every run and whether the tracker took it (`reconciled`). `gone` means the PID is absent, `pid_reused` means a registered PID now belongs to another process, and `unverified` means the process exists but its identity could not be checked.

Set `[workers] mode = "one-shot"` in `chief-of-stuff.toml` to require one-shot execution for every task worker. Interactive mode remains the default when the setting is absent. In an interactive workspace, pass `--one-shot` when the user requests one-shot for a particular task; this does not change the workspace setting. A direct request wins over the setting: in a one-shot workspace, pass `--interactive` when the user asks for a standing session for a particular task. The selected CLI runs once, with write access and no worker mailbox or plan gate. Claude uses print mode with automatic permissions and denies anything that would still prompt; Codex uses a writable sandbox with `-a never`; Cursor uses print mode with `--force`; Antigravity uses print mode with tool permissions preapproved. Standard input is closed, so an unavailable permission becomes a worker failure and human-review handoff instead of a stalled prompt. The workspace setting authorizes routine dispatch without per-task approval; a task-specific one-shot request authorizes that launch. Tasks whose File ownership paths overlap no running task launch together in the background, up to `max_concurrency`; overlapping ones wait for it to return. The coordinator selects ready tasks, reports progress and blockers, and keeps the tracker and board current. Lane gates and human-review holds still require their configured decisions. The worker reads the scoped Tasks and File ownership rows, writes a one-shot assignment into the worktree, and captures logs and a TOON report under `.chief-of-stuff/`. A worker must write `worker-result.toon` with `status: done`, `status: human_review`, or `status: relaunch`, a `reason`, `changes`, and the final suite counts (`passed`, `failed`, `skipped`, `xfailed`, or `null` when unknown). `relaunch` means the task's premise moved before the worker changed anything, such as its target pull request merging; the launcher sets the row back to ready with no hold, and the coordinator relaunches it in a fresh tree. A relaunch that left changes, or a second relaunch of the same task that day, becomes human review. A quota stop on a model is relaunched once per task and model per day and does not use the ordinary daily relaunch; a second one on the same model that day is human review. The quota-relaunch marker is a launcher-written Log line, so a worker's reason can never carry it. A missing result, nonzero exit, timeout, or dirty worktree after a claimed completion requires human review. A task whose File ownership starts with `none` is read-only: its worker is told not to edit, commit, push, merge or open a pull request, and a read-only run that left a commit on its branch or a changed file in its tree is human review, with the worker's own reason kept after the paths. The task is told this; nothing stops a worker that ignores it. The default limit is 60 minutes; set `--timeout-minutes N` for longer work. When the worker starts, the launcher sets the task's owner to the worker's name and its state to `running HH:MM`, adds the tree and branch to the task's File ownership row, and logs the launch. When the worker returns, the launcher sets the tracker row to `waiting` for the configured user. If review is needed, it adds the configured `human_review_label` while preserving the current numbered stage or GitHub Project Status, and posts an issue comment with the reason and partial changes when an issue exists. For tasks without an issue, the tracker Log carries the reason and changes. The printed report lists any tracker, label, or comment failures for the coordinator to reconcile. `--dry-run` previews the command without writing or launching.

Name each task class's suggested model and fallback order in the same TOML. Each entry is `runtime:model-id@effort`: the runtime is `claude`, `agy`, `codex` or `cursor`, the model ID is used verbatim, and `@high`, `@medium` or `@low` is optional (not for `cursor`, which takes no effort). The first entry is the suggested model; the rest is the order to fall back through when a model is unavailable or out of quota. Class names are free-form, like lane names.

```toml
[models.deep]
rotation = ["claude:opus@high", "codex:<model-id>@high"]
[models.implement]
rotation = ["claude:sonnet@medium", "codex:<model-id>@medium", "claude:haiku"]
[models.review]
rotation = ["codex:<model-id>@high", "claude:opus@high"]
```

`chief-of-stuff models --root . --class implement` prints the suggested entry. `--after <entry>` prints the next one, or exits nonzero when the rotation is exhausted. `--not-family <runtime>` skips that runtime's entries, for an independent review. `chief-of-stuff worker --class implement` launches the suggested entry when no `--runtime` or `--model` is given; explicit flags win. Effort is passed to Claude, Codex and Antigravity workers; Cursor takes none, so an entry or `--effort` that sets one for Cursor is refused. A bad runtime, an empty or duplicate rotation, or a bad effort is refused. Without `[models]`, the coordinator follows `chief-of-stuff-models.md`.

Stage launch defaults live beside the model tables in the settings TOML:

```toml
[stages.review]
agent = "reviewer"
skills = ["security-review"]
```

Each configured stage must appear in a lane's stages or the kanban map (including terminal stages). Both keys are optional. Claude workers launch with the stage's agent profile when the Coordinator block declares it with an `Agent:` line; `worker --type` or the engine persona's declared `Agent:` type takes precedence. Other runtimes receive only the skills. Every worker brief names the stage's skills and instructs the worker to load and follow them on demand. Invalid profile names, skill lists, unknown keys and unknown stages are refused.

Interactive Cursor workers start in [Plan mode](https://cursor.com/docs/cli/overview). Interactive Antigravity workers also start in plan mode. Interactive Codex workers start with a read-only sandbox for planning; after plan approval, use the assignment's `session_exec.py resume-codex` command to resume that session ID with write access and update its process registration. Codex documents `/plan` as a [developer command](https://learn.chatgpt.com/docs/developer-commands), but its CLI has no plan launch flag.

Every interactive worker assignment describes a five-minute mailbox check. Antigravity uses [Schedule or `/schedule`](https://antigravity.google/docs/slash-commands/), Cursor uses its in-session [`/loop`](https://cursor.com/docs/agent/overview), and Claude uses its native cron tools. [Codex CLI has no Scheduled interface](https://learn.chatgpt.com/docs/automations), so its workers check at the start of each turn and after each commit. The prompts keep the approval gate for each new task.

Workers can use the pinned `chief-of-stuff inbox wait --recipient <assigned-name> --timeout 300` for a bounded idle check. It prints unread messages as TOON as soon as they arrive, or `[]` after five minutes, without acknowledging them. A scheduled host check should run `chief-of-stuff inbox list --recipient <assigned-name> --unread` once per tick. These shared commands replace workspace-specific polling scripts under `.chief-of-stuff/`; a wait only runs while the agent's current tool call is active and does not wake a finished conversation.

New mailbox messages are stored as `.toon` files, and the structured results of `send`, `list`, `wait`, `read`, `drain`, and `status` default to TOON. Existing `.json` mailbox messages remain readable and can be acknowledged. Use `--format json` only for older consumers that still require JSON. `send --payload-toon '<TOON object>'` and `send --payload-file message.toon` accept TOON payloads; the existing JSON payload flags remain supported. `send --body-file PATH` reads the body from a UTF-8 file, or from stdin when the path is `-`; use it for bodies too large for one command-line argument.

Restart coordinators and workers launched from an older pinned release before new workers send TOON mail. An older inbox script only understands JSON and may quarantine a `.toon` message as malformed; new scripts can read both formats during the transition.

The Claude coordinator keeps its 15-minute full check and adds a five-minute mailbox check. Codex, Cursor and Antigravity coordinators have a session-scoped Python watcher that audits the inbox, workers and tasks every five minutes and notifies on findings. Antigravity and Cursor also start their native recurring mailbox prompts when available. Codex reconciles tracker and board state on its next turn because the CLI cannot wake an idle conversation.

The optional [Kanban walk](docs/kanban.md) maps tracker stages to configurable board columns in the workspace TOML. It syncs GitLab issue labels, GitHub issue labels, or a GitHub Project Status field, with `!!` as an independent human hold. Moves are previewed by default, and the audit reports drift without moving cards.

`chief-of-stuff pipeline --root /path/to/workspace --id N` reads a GitLab pipeline or GitHub Actions run. Use `--ref main` instead of `--id N` to read a branch's head and its CI status: GitLab uses the latest pipeline; GitHub aggregates the latest run of every workflow on that head and refuses a truncated list. Both use the workspace's `Backlog:` repository and host, including self-hosted GitLab. The TOON result contains `id`, `ref`, `sha`, `status`, and `url`; GitHub branch results use the newest run's `id` and `url` with the aggregate status. A head without a pipeline has `id: null`, `status: none`, and an empty URL. A successful read exits zero even when CI failed; unreadable forge state exits nonzero.

`chief-of-stuff pages --ensure --root /path/to/workspace` serves the pages and prints a status without opening or printing the URL. The pages are routes and render themselves: a request for `/` (the board), `/decisions`, `/decisions/<slug>` or `/issues/<n>` (a tracker issue's page; a 404 when no task names it) or `/issues/<n>/source` (its change's files and unified diff, read from the `[graph]` clone, with each review thread under its line from one small forge query for that change) re-renders the page's file when the tracker, `CLAUDE.md`, the settings TOML, a decision JSON or `.sources.json` is newer than it, and on a render error the server keeps the last good page with a one-line banner. Every served HTML page reloads itself when its file changes, and pauses with a notice and a button while the user has typed text in it. The rendered files themselves are not served; other files a session writes into the pages dir are, and `/all` lists them. A server thread runs `chief-of-stuff sources --refresh` every minute: one GraphQL call per forge for the issues and pull or merge requests today's tracker names, plus the workers, cached in `<pages dir>/.sources.json`. `--ensure` restarts a pages server older than the installed code. `chief-of-stuff board` still writes the board file by hand. Only the user opens the board when they want to view it.

`chief-of-stuff log --root /path/to/workspace "<text>"` appends `- HH:MM <text>` to today's tracker Log, stamped in the workspace zone. Every script-side tracker write, the one-shot launcher's included, holds the same lock on the tracker's directory, so no lock file appears in the workspace. `chief-of-stuff log --root /path/to/workspace --stage "<task name>" <stage>` sets that task's `stage` cell and appends `- HH:MM stage: <name> → <stage>` under the same lock; it refuses a name that matches no row or two, and a stage outside the task's lane. The board's Flow charts draw each task's stages from these lines, over the trackers of the last two days.

`chief-of-stuff tracker --root /path/to/workspace header` prints the lines above today's tracker's first heading, `sections` prints each heading's line number and name, `section <name>` prints one section as written, and `tasks [--not <state>] [--state <state>] [--issue <ref>]` prints one TOON row per task: name, owner, state, stage and issue. It writes nothing.

A decision's page is `<pages dir>/decision-<slug>.json`, served as `http://127.0.0.1:<port>/decisions/<slug>` and rendered on request; `chief-of-stuff decision --root /path/to/workspace --name <slug>` renders it by hand. The JSON fields are listed in `scripts/decision_page.py`.

The board and `notify.py sync` also read precise deadlines from tracker `## Decisions` rows: put `deadline: <name> YYYY-MM-DD HH:MM` in the `item` column, or `deadline: <name> HH:MM` for a time on that tracker's date. A later decision for the same name updates its marker, task horizon, and future notification warnings. A decision without an exact time supplies no instant. Run notification sync after a precise deadline decision changes.

## Verification

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/run.py --arm agent --golden --model opus --runs 3
python3 evals/run.py --runtime codex --arm agent --model gpt-6-astra --effort high --golden --case stale-clock
python3 evals/run.py --runtime cursor --arm agent --model <cursor-model> --case stale-clock
python3 evals/run.py --runtime agy --arm agent --model <agy-model> --case stale-clock
```

The eval runner shares fixture setup and graders across CLIs. Claude remains the default backend. Codex accepts the isolated calendar and peer MCP mocks through per-run config. The harness pins its fake `gh` executable by absolute path for backlog writes, even if an agent shell changes `PATH`. Cursor and Antigravity currently refuse cases that require those mocks, and headless backends refuse wait turns that depend on an idle conversation waking by itself. A refusal is recorded as unmeasured, never as a passing case.
