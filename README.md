# chief-of-stuff

A daily coordinator for Claude Code, Codex CLI, Cursor CLI, and Antigravity CLI. The workspace keeps its existing `CLAUDE.md` `## Coordinator` block. The coordinator routes work, keeps the tracker and board current, and automatically dispatches ready one-shot tasks and asks before new interactive sessions. Every decision it puts to the user links a page with the full context: the question, background, options, a recommendation and the default. With a self-hosted board, `chief-of-stuff decision` renders that page into the pages dir and prints its local URL. Without one, the page is a comment on the issue or pull request the decision is about. A reply asks one decision at a time.

## Install and launch

Install one command from this checkout:

```sh
python3 chief_of_stuff.py install --bin-dir /usr/local/bin
```

This writes a stable launcher in `/usr/local/bin` and a versioned release under `~/.local/share/chief-of-stuff/`. The system directory needs write permission only when first installing or explicitly replacing a different launcher; later installs update the user-owned `current` pointer. Use `--dry-run` to preview the paths without writing. Discover current commands by purpose and group with `chief-of-stuff --help`, or read the generated [command reference](docs/commands.md). Use `chief-of-stuff <command> --help` for each command's options.

Command help is the source of truth. The shared standard covers purpose (the argparse description), when not to use the command, options, output, exit codes, side effects and examples; each per-command help issue enables its check in `ENFORCED` in `evals/test_cli.py`. Retiring commands are excluded from discovery and the reference while their routes remain available until removal.

Regenerate the reference with `python3 chief_of_stuff.py docs > docs/commands.md` using Python 3.14, the CI version, with `requirements.txt` installed (store-backed commands import SQLAlchemy to print their help). The freshness test rejects stale output; resolve generated-file conflicts by regenerating, and edit the command's parser help instead of the generated Markdown.

Run the engine with `chief-of-stuff engine --root /path/to/workspace` (`--once` runs one pass). On macOS, `chief-of-stuff supervisor --root /path/to/workspace install` installs its persistent LaunchAgent; `status` inspects it and `uninstall` removes it. These three operations also work through `chief-of-stuff engine`. The workspace needs a configured backlog and kanban stage labels. Engine commands use the pinned dependency venv. Repeating supervisor install restarts the engine when the release path, plugin version or dependency pins change; a running loop prepares the selected release's dependency venv before re-executing its `engine` when the `current` pointer moves, and keeps running the working release if preparation fails. A running engine’s handoff preflight has a 60-second total budget; automatic startup retries bounded 20-minute waits for the shared supervisor lock and preparation has a 900-second budget; repaired venvs are published as new generations only after installation succeeds, preserving interpreters other workspaces still use. The LaunchAgent launches through a stable `venv-<key>.current` link, so a repair by any workspace reaches it without a reinstall while the pins and base Python stay the same. Failed release/pins preflights retry with persisted exponential backoff from 60 seconds to one hour; a busy supervisor lock defers preparation until a later pass. The engine follows `current` even when launched with `CHIEF_OF_STUFF_RELEASE`. A newer direct Claude plugin cache session requests a handoff to its validated sibling at a pass boundary; the engine reloads its LaunchAgent at that boundary so later restarts use the selected release. Automatic cache sessions upgrade an automatically selected engine to newer validated siblings. An explicit engine install pins its chosen cache release, including a downgrade, until another explicit install or uninstall; later automatic ensures cannot override it. Installed releases continue to follow their `current` pointer. The latest explicit install or uninstall is recorded before changing launchd so an interrupted operation can recover in order. A pass lasting over 24 hours produces an advisory that it may be stuck, not a failure verdict. Quota and outage waits persist across restarts and retry automatically with backoff. The loop keeps the configured board server alive, backing off failed starts up to 60 seconds, through the same engine LaunchAgent.

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

The plugin declares `datetime-inject@ai-additions` at `^1.0` as a dependency, so installation also installs the clock. Engine workers started as a local Claude CLI process check before launch that it is installed and enabled at user scope in their Claude account, including the selected `claude_as_profile`. A missing, disabled, or incompatible clock makes that local Claude entry unlaunchable: the rotation moves to another entry, and the launch is refused only when none is left. Codex, Cursor, Antigravity and T3-launched workers are not checked, so they still rotate. The clock plugin supplies one bracketed timestamp per prompt; chief-of-stuff ships no clock hook. To check delivery with two real worker prompts, run `CHIEF_CLOCK_LIVE=1 CHIEF_CLOCK_PROFILE=secondary python3 -m unittest evals.test_worker_clock.LiveClockTest -v` from a checkout with the Python requirements installed (omit `CHIEF_CLOCK_PROFILE` for the default account).

A running engine from an older release may not understand the upgrade request. `supervisor status` reports `upgrade_pending: true` while the registered loaded release differs from the selected release or an explicit install is still pending; ensure reports `engine: upgrade pending` and does not interrupt the running pass. An interrupted explicit install can remain pending indefinitely if the old engine does not understand the handoff. For a marker-unaware engine, arrange a maintenance window, stop new work and verify the current pass has completed and no new pass can start before running `chief-of-stuff supervisor --root /path/to/workspace install` from the desired release. That explicit command restarts the agent and can interrupt work if it is not actually idle. Missing or stale status alone does not establish idleness.

The authoritative install/uninstall recovery record is stored in a private engine data directory outside the workspace. The data directory must not be inside the workspace. Releases and their ancestors must be trusted outside the workspace; release trees overlapping the workspace or containing links outside the release tree are rejected before preparing dependencies or starting their code. This protects against workspace-only writes, not an attacker who can modify the release cache or private engine data. Older workspace-only pending install records are not trusted or replayed; if one was interrupted before this upgrade, arrange a confirmed idle maintenance window and run an explicit install again.

For a store-backed workspace, the plugin session startup hook starts an engine ensure in the background. The versioned launcher does the same for every runtime. First-time dependency preparation may take up to 900 seconds; errors are recorded in `.chief-of-stuff/engine-ensure.log`. An explicitly stopped engine remains disabled until installed again.

The launcher starts even when the selected CLI does not expose the configured `Calendar tool:`. The coordinator reports the missing tool, leaves calendar facts unverified, and continues work that does not depend on the calendar. To enable calendar reads, configure an equivalent calendar tool in that CLI.

If a workspace requires workers to use only the configured `User:` name, add `- Identity: user-name-only` to its `## Coordinator` block. Without that line, assignments make no claim about aliases or names found in workspace files.

The coordinator's PARA filing rule applies only when the workspace `CLAUDE.md` explicitly defines the `Projects/`, `Areas/`, `Resources/`, and `Archives/` homes. Other workspaces do not gain those folders or automatic filing moves.

Notifications are off unless the workspace settings explicitly set `[notify] adapter = "md-notify"`. The default queue path for that adapter is `notifications/NOTIFICATIONS.md` under the workspace; set `queue` to use another path.

Enabled notifications have an independent macOS user service, ensured before coordinator launches on every runtime. It reconciles deadline and day notifications when configuration or tracker inputs change (checked every five seconds), every minute, and after restart, even with the coordinator closed. Use `chief-of-stuff notify --root . status` for health, `stop` to unload it, and `ensure` to restore it. Manual `sync` remains available for diagnostics. Queue writes use a shared lock; concurrent acknowledgment preservation requires the companion md-notify transaction update and a rebuilt app. See [notification lifecycle and compatibility](docs/notify.md).

For a GitHub `Backlog:` line, the pinned `chief-of-stuff backlog --create <title> --body <text> --commit` files issues through `gh` without a personal plugin. It supports native `--parent` and `--blocked-by` relationships. Writes preview by default; a workspace can still set its own issue content rules. See [backlog setup](docs/backlog.md) for GitHub and GitLab configuration.

An optional `[workflow] architecture_reviewer = "<session-name>"` in the workspace TOML routes filed architecture findings to that session. Without it, no particular person or agent is assumed.

An optional `[workflow] reviewer_session = "<session-name>"` routes pull requests to a named reviewer. Without it, the coordinator asks the user how to review a pull request instead of inventing a reviewer session.

Every reviewer reports in one shape, [`skills/review-pipeline/review-report.md`](skills/review-pipeline/review-report.md), whatever its runtime, because Triage reads each finding's axis, severity, confidence and suggestion off it. Every worker assignment points a reviewing worker at it.

`[workflow] delivery = "pull-request"` remains the default. The engine owns merges through its automated review and CI gate. Legacy `merge_owner = "user"`, `"approval"` and `"worker"` values normalize to `"coordinator"`; none waits on the owner. `approver` is an optional review-thread filter. `merge-approved` is a compatibility readiness listing; run the engine to execute a merge. Set `delivery = "branch"` for branch handoff. Code tasks finish only after integration into main and a green suite there.

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
worker_launcher = "t3" # optional: engine Claude workers start as T3 threads; "cli" (the default) is a local process
require_green_tests = true # optional: hold one-shot and engine completions with unknown or non-green suite counts
transcript_max_bytes = 1048576 # optional: retained transcript event bytes per engine attempt; default 1 MiB
rules = ["Never skip or weaken tests."] # optional: standing rules for every one-shot and engine worker

[workers.t3_providers] # optional, T3 launcher only: the T3 provider instance each claude_as_profile runs as
secondary = "cl2"
```

The engine retains Claude CLI worker transcripts in its ORM store, keyed by issue, stage and attempt. Ordered worker, tool, user, error and gap events remain readable after the worker exits, the engine restarts or the original log disappears. `Store.transcript(issue_key, stage, attempt)` reads that retained history without accessing the runtime or its files; it returns `None` before capture. T3 thread transcripts and other runtimes are not captured yet.

`transcript_max_bytes` must be a positive integer and is pinned on the attempt's first capture. It caps the serialized event array, including JSON escaping and separators; fixed-size identity, cursor and loss metadata are additional. Once full, capture keeps the prefix and drops all subsequent normalized events, including tools and gap events, counting them in `dropped_events`. A source record larger than the cap is skipped without decoding and counted separately in `dropped_records`, since its event count is unknown. `capped`, `gap_count` and `last_gap` remain visible after the buffer fills. Partial records wait for completion; a partial tail at exit is marked as a gap. Missing files are marked and retried. Replacement, truncation, rewritten history or changed session identity halts capture with a gap, preserving the original attempt's history.

The engine's stall threshold is configurable:

```toml
[workers.stall]
after_minutes = 20 # positive whole minutes; default 20
action = "wait"    # the only supported action
max_restarts = 2   # accepted for future recovery; has no effect yet
```

At the threshold, the engine reports `stalled` and leaves the worker running. This is a tentative signal based on worktree Git commit time, not a complete measure of worker activity: a worker can be productive without committing, and an unreadable tree has no verified progress. Automatic restart is not implemented, and `action = "restart"` is rejected rather than silently ignored. Safe restart requires reliable per-session activity and a verified stop and recovery path that preserves work and attempt history.

`[workers] rules` is an ordered list of non-blank, single-line strings without control or Unicode format characters or `(via ` markers, each at most 2400 characters; surrounding spaces are preserved. There are at most 64 rules and 6000 characters in all. The engine also checks each configured stage's complete assignment budget at startup, including maximum row fields, persona rules, skills, architecture and suite instructions. One-shot assignments check the complete task against the assignment cap before launch. Every one-shot and engine assignment puts these rules in its header, before the task. Interactive launches do not carry them. Omitting `rules` or setting it to `[]` leaves the assignment unchanged.

With `claude_as_profile` set, every Claude-runtime worker, interactive or one-shot, launches through `claude-as run <profile>` instead of a bare `claude`, so it runs under that account profile. `claude-as` must be on the login shell's PATH, or the launch is refused. Codex, Cursor and Antigravity workers are unaffected.

One-shot results include `passed`, `failed`, `skipped`, and `xfailed` fields from the final suite run, as non-negative integers or `null` when unknown. Workers name the suite command in `changes`; earlier failing TDD runs do not contribute to these counts. Reports retain the counts and explicitly name missing or invalid ones. Set `[workers] require_green_tests = true` to turn `done` into `human_review` unless all counts are known, `passed > 0`, and `failed`, `skipped`, and `xfailed` are zero. The engine applies that same gate when it collects a worker report: pass the four counts to `report`, and a non-green or unknown suite holds the issue for human review. The engine retries a failed hold-label write without relaunching work. Removing the published hold accepts that completion and lets the workflow continue. The setting defaults to false, so older results without counts still complete in workspaces that have not opted in. Tasks that run no suite report unknown counts and need human review in an opted-in workspace.

Set `[workers] teardown = "python3 tools/stack.py down"` to clean up each engine worker's local containers, volumes and VMs. This setting is engine-only. The command runs in the workspace root through the configured interactive login shell, so a relative script is the workspace's copy. `CHIEF_OF_STUFF_WORKER` is the engine worktree's directory name, `CHIEF_OF_STUFF_WORKSPACE` is the workspace root, and `CHIEF_OF_STUFF_WORKTREE` is the worker's worktree. The engine runs teardown when collecting an exited or lost worker, and after cancelling a worker whose start could not be verified. Commands have a separate 120-second limit. A cleanup failure appears in the engine's issue log; a successful worker keeps its `done` outcome. One-shot launchers and adoption do not run this command; one-shot cleanup is unenforced until engine cutover.

The optional `[workers] teardown_list` setting is accepted and validated for the planned cleanup guard; it has no consumer yet. With both settings absent, no stack commands run.

With `launcher = "herdr"`, each interactive worker starts in its own tab of a dedicated herdr session (default `chief-of-stuff`, session name rule `[a-z0-9][a-z0-9_-]{0,31}`), which must already be running with an active workspace. After creating the tab, the launcher uses its returned root pane id with `herdr --session <session> pane run <pane-id> <command words>` to run the worker wrapper through the pane's shell, quoting each command word with `shlex.quote`. The typed line omits the `env -C <root> PATH=... CHIEF_OF_STUFF_WORKSPACE=...` prefix tmux and Ghostty use: the tab is created with `--cwd`, the pane shell already has the user's PATH, and the workspace reaches the worker through `tab create --env CHIEF_OF_STUFF_WORKSPACE=<root>`. A typed line over 900 bytes would be cut by the pane's tty, so the launcher refuses it, before creating a tab, instead of reporting a launch that did not happen. The launcher's own herdr calls carry no `HERDR_*` variables; herdr sets its own variables inside each pane, and isolation comes from the dedicated session's own socket. Any launch failure after a valid tab id is received attempts to close that tab. Because herdr has no `session create`, the launcher first asks the configured session's roster (`herdr --session <session> session list --json`); when a readable roster shows the session is not running, the launch is refused — naming `[workers] herdr_session` and the one-time `herdr --session <session>` that begins the session — before any tab is created. A roster request that fails or cannot be read is never treated as "not running"; `tab create` reports it. After `pane run` submits the line, the launcher waits up to 30 seconds for the worker's own registration to appear (through herdr the Claude wrapper registers too, into `<workspace>/.chief-of-stuff/sessions/<name>.json`) and treats a start that never registered as a failed start: it reports the failure, closes the created tab, and leaves the worktree reusable — a cut line or a shell that never ran the command is no longer reported as a start, and a registration left by an earlier worker of the same name does not vouch for the new one.

The settings' `[repos]` table maps a repository name to a path relative to the workspace root (`alder = "repo"`). `chief-of-stuff worktree --clone <name>` cuts from that entry; a `--clone` that is no entry's name is a path, as before. The coordinator passes the name when the table names the repository, otherwise the path. Settings that cannot be read are a refusal.

`chief-of-stuff worker --check --root . --task <name>` checks the Tasks row only: it needs no `--cwd`, makes no worktree, starts nothing and prints the assignment or `refused: <why>`. Add `--name <session>` for a standing row and `--one-shot` for a one-shot task; the other launch flags are ignored. For a ready interactive dispatch, `chief-of-stuff worker` starts a separate worktree terminal with `--runtime claude|agy|codex|cursor`. Claude remains the default. Ghostty tabs are the default launcher. Set `[workers] launcher = "tmux"` in the workspace TOML named by the `Settings:` line, or pass `--launcher tmux`, to open a new window in an existing tmux session. A launch refuses a missing tmux executable or tmux server. `--launcher ghostty` overrides the setting for one dispatch. The assignment is written to the worktree before the terminal opens; each worker reads it through a fixed bootstrap prompt. Claude uses native session messaging. Other workers register and report through the workspace file mailbox, using their assigned name and worktree. A launcher wrapper loads the configured interactive login shell for every worker and records registered worker processes under `.chief-of-stuff/sessions/` (through herdr the Claude wrapper registers too, for the launcher's start verification). `chief-of-stuff processes --root /path/to/workspace` checks all registered workers in one batch and lists each live one-shot run (its launcher PID, its task when today's or yesterday's tracker holds it, and its tree's directory name) as rows after the workers in the same list, marked `kind: one-shot`, and prints TOON. Pass space- or comma-separated PIDs to check a specific batch, including unregistered processes. `chief-of-stuff result --root /path/to/workspace` prints a finished one-shot's report by `--task`, or without it lists every run and whether the tracker took it (`reconciled`). `gone` means the PID is absent, `pid_reused` means a registered PID now belongs to another process, and `unverified` means the process exists but its identity could not be checked.

Set `[workers] mode = "one-shot"` in `chief-of-stuff.toml` to require one-shot execution for every task worker. Interactive mode remains the default when the setting is absent. In an interactive workspace, pass `--one-shot` when the user requests one-shot for a particular task; this does not change the workspace setting. A direct request wins over the setting: in a one-shot workspace, pass `--interactive` when the user asks for a standing session for a particular task. The selected CLI runs once, with write access and no worker mailbox or plan gate. Claude uses print mode with automatic permissions and denies anything that would still prompt; Codex uses a writable sandbox with `-a never`; Cursor uses print mode with `--force`; Antigravity uses print mode with tool permissions preapproved. Standard input is closed, so an unavailable permission becomes a worker failure and human-review handoff instead of a stalled prompt. The workspace setting authorizes routine dispatch without per-task approval; a task-specific one-shot request authorizes that launch. Ready tasks launch together in the background, up to `max_concurrency`. The coordinator selects ready tasks, reports progress and blockers, and keeps the tracker and board current. Legacy lane gates and configured hold stages do not create owner waits. Architectural decisions and two failures park the issue with research, options and a recommendation; a split or retry-exhausted decision trio waits on the owner. The worker reads the scoped Tasks row, writes a one-shot assignment into the worktree, and captures logs and a TOON report under `.chief-of-stuff/`. A worker must write `worker-result.toon` with `status: done`, `status: human_review`, or `status: relaunch`, a `reason`, `changes`, and the final suite counts (`passed`, `failed`, `skipped`, `xfailed`, or `null` when unknown). `relaunch` means the task's premise moved before the worker changed anything, such as its target pull request merging; the launcher sets the row back to ready with no hold, and the coordinator relaunches it in a fresh tree. A relaunch that left changes, or a second relaunch of the same task that day, becomes human review. A quota stop on a model is relaunched once per task and model per day and does not use the ordinary daily relaunch; a second one on the same model that day is human review. The quota-relaunch marker is a launcher-written Log line, so a worker's reason can never carry it. A missing result, nonzero exit, timeout, or dirty worktree after a claimed completion requires human review. The default limit is 60 minutes; set `--timeout-minutes N` for longer work. When the worker starts, the launcher sets the task's owner to the worker's name and its state to `running HH:MM`, and logs the launch. When the worker returns, the launcher sets the tracker row to `waiting` for the configured user. If review is needed, it adds the configured `human_review_label` while preserving the current numbered stage or GitHub Project Status, and posts an issue comment with the reason and partial changes when an issue exists. For tasks without an issue, the tracker Log carries the reason and changes. The printed report lists any tracker, label, or comment failures for the coordinator to reconcile. `--dry-run` previews the command without writing or launching.

Name each task class's suggested model and fallback order in the same TOML. Each entry is `runtime:model-id@effort`: the runtime is `claude`, `agy`, `codex` or `cursor`, the model ID is used verbatim, and `@high`, `@medium` or `@low` is optional (not for `cursor`, which takes no effort). The first entry is the suggested model; the rest is the order to fall back through when a model is unavailable or out of quota. Class names are free-form, like lane names.

```toml
[models.deep]
rotation = ["claude:opus@high", "codex:<model-id>@high"]
[models.implement]
rotation = ["claude:sonnet@medium", "codex:<model-id>@medium", "claude:haiku"]
[models.review]
rotation = ["codex:<model-id>@high", "claude:opus@high"]
```

`chief-of-stuff models --root . --class implement` prints the suggested entry. `--after <entry>` prints the next one, or exits nonzero when the rotation is exhausted. `--not-family <runtime>` skips that runtime's entries, for an independent review. `chief-of-stuff worker --class implement` launches the suggested entry when no `--runtime` or `--model` is given; explicit flags win. Effort is passed to Claude, Codex and Antigravity workers; Cursor takes none, so an entry or `--effort` that sets one for Cursor is refused. A bad runtime, an empty or duplicate rotation, or a bad effort is refused. Without `[models]`, the coordinator follows `chief-of-stuff-models.md`. An entry without a runtime prefix means Claude.

The engine launches each stage from the owner's configured class rotation: plan from `deep`, implement and fix from `implement`, review from `review`. Fresh workers fan out in round robin by default (`pick = "round_robin"`). Each class's cursor is stored in the workspace database; it survives engine restarts and reserves turns atomically across concurrent launches. A recovered launch keeps its reserved entry. Set `pick = "first"` to always start at the first eligible entry, or `pick = "random"` to sample eligible entries uniformly. Under every strategy, a retry follows the previous attempt's recorded entry, wrapping at the end and skipping unavailable entries. Fix work continues the implementation rotation. Selection never reads performance records.

A Codex, Cursor or Antigravity entry is launchable only when its CLI is on the login PATH and the forge protects the default branch (§13); others are logged as skipped. Entries whose account is near its cap are also skipped, using the existing local limit probe, the Claude account selected by `claude_as_profile`, and `[limits] warn_at`. Unknown limits do not exclude an entry. If every launchable entry is near its cap, the launch is refused; the persona does not bypass that cap. With no launchable configured entry, the engine retains the persona fallback, subject to the same limit check. Model effort, worker modes and launchers remain workspace settings; retry budgets, tuning and workflow selection remain policy data and the configured policy overlay.

`chief-of-stuff performance --since YYYY-MM-DD [--until YYYY-MM-DD] [--by day|week]` reports raw outcome counts and median whole minutes by runtime, model, effort, declared agent type and the issue's workflow. The workflow is snapshotted for each attempt, including failed starts, so later policy or issue changes cannot rewrite its classification. Older segments keep an unknown workflow (`-`). Add `--raw` for JSON with each attempt's identity, workflow, start/end timestamps, original outcome, completed/human-review/relaunched flags and individual whole minutes. An open attempt has no duration. A relaunch flag means attempt greater than one, as in the existing report. These records define no success rate and do not enable automatic tuning.

The decision trio draws its models from a `[models.trio]` rotation, with `pick = "first"` (the default) or `pick = "random"`. The worker round-robin cursor does not change this independent draw. A consult takes three distinct launchable entries, on three different runtimes when the rotation has them, so a unanimous verdict is three independent judgements. With fewer, it reuses what it has and records `diverse: false` on the decision. Each vote records the runtime and model that cast it. Every consult is read-only: Claude runs with no tools under `--safe-mode`, Codex in its `read-only` sandbox, Cursor in `--mode ask` and Antigravity in `--mode plan`, each in a fresh empty directory. A host with no read-only mode cannot sit in the trio. A quota stop moves that member to an entry no other member holds; when every entry has stopped, the owner decides. Without `[models.trio]`, each trio persona uses its own `model` from the policy.

```
R := a [models.trio] rotation is set      D := three distinct entries on three runtimes can launch
W := a consult can write      Q := every entry stopped on quota      O := the owner decides

R ∧ D → diverse: true; R ∧ ¬D → diverse: false          [#879]
¬W                                                     [every host runs read-only, §6]
Q → O                                                  [§6: an exhausted retry]
¬R → each persona's own model
∴ the trio rotates like workers, stays read-only, and falls back to the owner
```

Stage launch defaults live beside the model tables in the settings TOML:

```toml
[stages.review]
agent = "reviewer"
skills = ["security-review"]
```

Each configured stage must appear in a lane's stages or the kanban map (including terminal stages). Both keys are optional. Claude workers launch with the stage's agent profile when the Coordinator block declares it with an `Agent:` line; `worker --type` or the engine persona's declared `Agent:` type takes precedence. Other runtimes receive only the skills. Every worker brief names the stage's skills and instructs the worker to load and follow them on demand. Invalid profile names, skill lists, unknown keys and unknown stages are refused.

Interactive workers start with write access and plan before acting, without a plan approval stage. The coordinator selects and launches ready work in either execution mode. Worker safeguards keep merges, protected pushes and stage writes with the engine; non-Claude engine entries still need forge branch protection before launch.

Every interactive worker assignment describes a five-minute mailbox check. Antigravity uses [Schedule or `/schedule`](https://antigravity.google/docs/slash-commands/), Cursor uses its in-session [`/loop`](https://cursor.com/docs/agent/overview), and Claude uses its native cron tools. [Codex CLI has no Scheduled interface](https://learn.chatgpt.com/docs/automations), so its workers check at the start of each turn and after each commit. New tasks proceed without an owner approval gate. A first failure retries on the next agent; two failures across the issue park it, including relaunches after stalls.

Workers can use the pinned `chief-of-stuff inbox wait --recipient <assigned-name> --timeout 300` for a bounded idle check. It prints unread messages as TOON as soon as they arrive, or `[]` after five minutes, without acknowledging them. A scheduled host check should run `chief-of-stuff inbox list --recipient <assigned-name> --unread` once per tick. These shared commands replace workspace-specific polling scripts under `.chief-of-stuff/`; a wait only runs while the agent's current tool call is active and does not wake a finished conversation.

New mailbox messages are stored as `.toon` files, and the structured results of `send`, `list`, `wait`, `read`, `drain`, and `status` default to TOON. Existing `.json` mailbox messages remain readable and can be acknowledged. Use `--format json` only for older consumers that still require JSON. `send --payload-toon '<TOON object>'` and `send --payload-file message.toon` accept TOON payloads; the existing JSON payload flags remain supported. `send --body-file PATH` reads the body from a UTF-8 file, or from stdin when the path is `-`; use it for bodies too large for one command-line argument.

Restart coordinators and workers launched from an older pinned release before new workers send TOON mail. An older inbox script only understands JSON and may quarantine a `.toon` message as malformed; new scripts can read both formats during the transition.

The Claude coordinator keeps its 15-minute full check and adds a five-minute mailbox check. Codex, Cursor and Antigravity coordinators have a session-scoped Python watcher that audits the inbox, workers and tasks every five minutes and notifies on findings. Antigravity and Cursor also start their native recurring mailbox prompts when available. Codex reconciles tracker and board state on its next turn because the CLI cannot wake an idle conversation.

The optional [Kanban walk](docs/kanban.md) maps tracker stages to configurable board columns in the workspace TOML. It syncs GitLab issue labels, GitHub issue labels, or a GitHub Project Status field, with `!!` as an independent human hold. Moves are previewed by default, and the audit reports drift without moving cards.

`chief-of-stuff pipeline --root /path/to/workspace --id N` reads a GitLab pipeline or GitHub Actions run. Use `--ref main` instead of `--id N` to read a branch's head and its CI status: GitLab uses the latest pipeline; GitHub aggregates the latest run of every workflow on that head and refuses a truncated list. Both use the workspace's `Backlog:` repository and host, including self-hosted GitLab. The TOON result contains `id`, `ref`, `sha`, `status`, and `url`; GitHub branch results use the newest run's `id` and `url` with the aggregate status. A head without a pipeline has `id: null`, `status: none`, and an empty URL. A successful read exits zero even when CI failed; unreadable forge state exits nonzero.

`chief-of-stuff pages --ensure --root /path/to/workspace` serves the pages and prints a status without opening or printing the URL. The pages are routes and render themselves: a request for `/` (the board), `/decisions`, `/decisions/<slug>` or `/issues/<n>` (a tracker issue's page; a 404 when no task names it) or `/issues/<n>/source` (its change's files and unified diff, read from the `[graph]` clone, with each review thread under its line from one small forge query for that change) re-renders the page's file when the tracker, `CLAUDE.md`, the settings TOML, a decision JSON or `.sources.json` is newer than it, and on a render error the server keeps the last good page with a one-line banner. Every served HTML page reloads itself when its file changes, and pauses with a notice and a button while the user has typed text in it. The rendered files themselves are not served; other files a session writes into the pages dir are, and `/all` lists them. A server thread refreshes workers every minute and caches the results in `<pages dir>/.sources.json`. GitHub issue and pull request reads run at most once every ten minutes automatically, in small bounded GraphQL pages; GitLab forge reads continue every minute. Failed or incomplete reads retain the last good panel and report an error. `chief-of-stuff sources --refresh` requests an immediate refresh. `--ensure` replaces a server from an explicitly selected release, including a different build with the same version or one without a release identity. A stale caller keeps an unselected newer, equal-version, or unordered server. It never moves the board to another port, and never signals a process the pid file names unless that process is the pages server on the board's port; a pid file naming a dead or unrelated process is dropped before a fresh start. A workspace with a board keeps its board across restarts with `chief-of-stuff pages install --root /path/to/workspace` (macOS): one LaunchAgent supervises the board like the engine does, and `uninstall` removes it while leaving the running server alone. The independent board LaunchAgent can be installed alongside the engine and remains a fallback after engine uninstall. It monitors release handoffs and resumes the previous supervisor if a successor fails; both supervisors coordinate through the board lock. `chief-of-stuff pages status` reports the board (`serving`, `down`, a foreign server or not configured) apart from the engine (`running` or `missing`), the LaunchAgent that restores the board after a restart, and whether the pid file names a live server. `chief-of-stuff board` still writes the board file by hand. Only the user opens the board when they want to view it.

For a board LaunchAgent installed by an earlier draft of this supervisor, rerun `chief-of-stuff pages install --root /path/to/workspace` after upgrading. Releases predating the readiness handshake cannot be adopted as successors; the working supervisor keeps serving if `current` is rolled back to one of them.

```
B := the workspace has a Board line      E := the engine's LaunchAgent is installed
A := a board LaunchAgent is installed    S := the pid file names `pages.py serve` on the board's port
K := a process is signalled              M := the board moves to another port

B ∧ ¬E → A may be installed                   [#935: `pages install`]
E ∧ A → A                                     [the installed board fallback remains when the engine is installed]
K → S                                         [#935: a dead or reused pid is dropped, never signalled]
¬M                                            [the URL is the board's address]
∴ a configured board comes back after a restart on its own port, and nothing else is stopped
```

`chief-of-stuff log --root /path/to/workspace "<text>"` appends `- HH:MM <text>` to today's tracker Log, stamped in the workspace zone. Every script-side tracker write, the one-shot launcher's included, holds the same lock on the tracker's directory, so no lock file appears in the workspace. `chief-of-stuff log --root /path/to/workspace --stage "<task name>" <stage>` sets that task's `stage` cell and appends `- HH:MM stage: <name> → <stage>` under the same lock; it refuses a name that matches no row or two, and a stage outside the task's lane. The board's Flow charts draw each task's stages from these lines, over the trackers of the last two days.

`chief-of-stuff tracker --root /path/to/workspace header` prints the lines above today's tracker's first heading, `sections` prints each heading's line number and name, `section <name>` prints one section as written, and `tasks [--not <state>] [--state <state>] [--issue <ref>]` prints one TOON row per task: name, owner, state, stage and issue. It writes nothing.

A decision's page is `<pages dir>/decision-<slug>.json`, served as `http://127.0.0.1:<port>/decisions/<slug>` and rendered on request; `chief-of-stuff decision --root /path/to/workspace --name <slug>` renders it by hand. The JSON fields are listed in `scripts/decision_page.py`.

On an engine workspace every decision in `store.db` also has a page, `http://127.0.0.1:<port>/decisions/<id>`, rendered from the store on request with no authored file. `<id>` is the id of the decision's first row, and any later row's id shows the same page; a path of digits is always a store id, so an authored slug is never only digits. The page shows the question, the options, the recommendation and the trio's outcome, what happens if no one answers, who it waits on, its issue, and every row of the decision, newest first. An open or blocked decision carries the answer form. An answered one shows the answer and who gave it, and keeps only Reverse, which takes your reason and appends a reversal for the engine to apply. A form posted from the page returns to it. An unknown id is a 404. The `/decisions` list and each task page link every decision to its page.

```
o := the decision is open or blocked      a := the decision is answered
F := its page carries the answer form     R := its page carries Reverse, which appends a reversal intent
W := the recorded answer is overwritten   x := the id names a decision row
1.  o → F                [#945; architecture §6]
2.  a → ¬F ∧ R           [#945, #971; architecture §6]
3.  R → ¬W               [§6: rows are append-only]
4.  ¬x → 404             [#945]
∴   an open decision is answered on its page, an answered one is only reversed there and never overwritten, and an unknown id has no page
```

The board and `notify.py sync` also read precise deadlines from tracker `## Decisions` rows: put `deadline: <name> YYYY-MM-DD HH:MM` in the `item` column, or `deadline: <name> HH:MM` for a time on that tracker's date. A later decision for the same name updates its marker, task horizon, and future notification warnings. A decision without an exact time supplies no instant. Run notification sync after a precise deadline decision changes.

## Workspace policy

The engine runs the shipped policy, `assets/policy.toml`, unless the workspace names an overlay: set `[policy] overlay = "policy.toml"` in the workspace TOML, with the path relative to the workspace root. The overlay's keys are merged over the shipped policy. A table merges key by key, and a field is replaced whole. For example, `[stages.review.policy] architect = false` stops architect requests in that workspace only. An overlay changes keys of tables the shipped policy has: a persona, stage, room or workflow it does not have is refused by name, and so is an unknown key. A named overlay that is missing is refused at startup; the engine never falls back to the shipped policy. The engine, `init` and the board load the same merged policy, and the engine logs `engine: policy changed: <fingerprint>` when the merged policy differs from the one the workspace last ran.

```
O := the workspace names an overlay      F := the overlay file exists      K := every key and table it names is known
P := the merged policy runs

O ∧ F ∧ K → P              [#827: keys merged over the shipped policy]
O ∧ ¬F → ¬P                [a refusal, never a silent fallback]
O ∧ ¬K → ¬P                [refused by name]
¬O → the shipped policy runs
∴ a workspace runs a known policy or does not start
```

## Engine worker asks

An operator can answer a stored worker ask with `chief-of-stuff work "Issue title" --root . --issue 12 --answer-ask 7 --answer "Proceed" --commit`. The ask must belong to that issue; omitting `--commit` previews the answer. Workers have no answer-writing tool.

## Verification

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/run.py --arm agent --golden --model opus --runs 3
python3 evals/run.py --runtime codex --arm agent --model gpt-6-astra --effort high --golden --case stale-clock
python3 evals/run.py --runtime cursor --arm agent --model <cursor-model> --case stale-clock
python3 evals/run.py --runtime agy --arm agent --model <agy-model> --case stale-clock
```

The eval runner shares fixture setup and graders across CLIs. Claude remains the default backend. Codex accepts the isolated calendar and peer MCP mocks through per-run config. The harness pins its fake `gh` executable by absolute path for backlog writes, even if an agent shell changes `PATH`. Cursor and Antigravity currently refuse cases that require those mocks, and headless backends refuse wait turns that depend on an idle conversation waking by itself. A refusal is recorded as unmeasured, never as a passing case.

Eval scenarios superseded by the autonomy change carry `superseded_by` in `case.json`. Live selection omits them; their fixtures remain available for mechanical grader checks. `autonomy-*` cases cover the replacement behavior. Run Python checks with the plugin's pinned dependency venv (see `docs/plans/issue-1030.md`), not a dependency-free system interpreter.

With `worker_launcher = "t3"`, each engine Claude worker is a thread in the running T3 Code app (T3 is a launcher, not a runtime), started through the `t3` CLI (`$T3CODE_CLI_PATH`, else the login shell's `t3`). The engine registers the exact worker tree as a T3 project, writes the report and ask channel, the agent and the guard hooks into the tree as git-excluded `.mcp.json` and `.claude/settings.local.json`, and starts the thread with the rotation entry's model. The thread id and its `t3code://threads/<environment>/<thread>` link are kept in the run record, and the board, issue and workers pages link to the thread. Thread state determines liveness, including after a polling process dies. Cancellation interrupts the remote thread through the already-pinned MCP client using a temporary, scoped CLI session; an unconfirmed cancellation preserves the run and its tree. T3 cannot restrict a session to the user's own settings, so a tree that brings its own `.mcp.json` or `.claude/` config is refused. Engine-owned channel files can be rebound on a later attempt only when their contents still match ownership evidence outside the tree. The forge must protect the default branch and the branch a request targets. T3 currently supports Claude only: unsupported rotation entries are skipped, a rotation with no supported entry is refused, and `claude_as_profile` selects its T3 provider instance through `[workers.t3_providers]` (no profile runs as T3's default `claudeAgent`); a profile with no mapping, or a mapped instance T3 does not have, refuses the launch with an error naming it, and the instance is passed with `--model`, which `t3 thread start` requires beside `--provider`. An operator message to a T3 worker goes through `t3 thread send` as the thread's next turn and is recorded as sent to the thread, never as read; a CLI refusal fails it, and a timeout leaves it unconfirmed and unsent twice. Registered worker projects and their thread history are retained; removing a project with `--force` deletes its threads and breaks their links.
