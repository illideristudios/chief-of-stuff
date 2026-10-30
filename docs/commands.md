# Command reference

<!-- Generated from argparse help. Do not edit by hand.
Regenerate (also to resolve conflicts):
python3 chief_of_stuff.py docs > docs/commands.md
-->

## Engine

### `engine`

```text
usage: engine.py [-h] [--root ROOT] [--interval INTERVAL] [--once]
                 [{install,uninstall,status,ensure,ensure-background,ensure-retry,commit-handoff}]

Drive one workspace's reconcile plan, under a singleton lock (1.0 M3).

positional arguments:
  {install,uninstall,status,ensure,ensure-background,ensure-retry,commit-handoff}
                        manage the engine's LaunchAgent

options:
  -h, --help            show this help message and exit
  --root ROOT           workspace holding store.db and CLAUDE.md
  --interval INTERVAL   seconds between passes (default 60)
  --once                run one pass and exit
```

### `supervisor`

```text
usage: supervisor.py [-h] [--root ROOT]
                     {install,uninstall,status,ensure,ensure-background,ensure-retry,commit-handoff}

The engine's launchd LaunchAgent and the venv it runs in, plus the LaunchAgent lifecycle
notify_service shares.

positional arguments:
  {install,uninstall,status,ensure,ensure-background,ensure-retry,commit-handoff}

options:
  -h, --help            show this help message and exit
  --root ROOT           workspace root the engine serves
```

### `worktree`

```text
usage: make_worktree.py [-h] --type AGENT_TYPE --name NAME [--branch BRANCH] [--from-local]
                        [--from-branch BRANCH] [--root ROOT] --clone CLONE

Create a checked worktree and branch for a dispatch.

options:
  -h, --help            show this help message and exit
  --type AGENT_TYPE     an agent type from the Coordinator block
  --name NAME           the worktree directory name, one path segment
  --branch BRANCH       the new branch, cut from origin/main unless --from-local or --from-branch
  --from-local          cut from local main, skipping the fetch of origin/main
  --from-branch BRANCH  check an existing remote branch out into the new tree instead of cutting
                        one (#62)
  --root ROOT           workspace root holding CLAUDE.md
  --clone CLONE         the repository the worktree belongs to: a name from [repos] in the
                        workspace settings, or the repository's path
```

## Intake

### `init`

```text
usage: init_workspace.py [-h] --root ROOT --user USER --timezone TIMEZONE [--daily-dir DAILY_DIR]
                         [--worktrees WORKTREES] [--agent NAME:task|standing]
                         [--launcher {ghostty,tmux}] [--worker-mode {interactive,one-shot}]
                         [--board-port BOARD_PORT] [--github-repo GITHUB_REPO |
                         --gitlab-host GITLAB_HOST] [--gitlab-project GITLAB_PROJECT]
                         [--gitlab-token-env GITLAB_TOKEN_ENV] [--dry-run]

Configure a workspace's forge, Coordinator block and engine settings.

options:
  -h, --help            show this help message and exit
  --root ROOT           existing workspace directory
  --user USER           name to show as the workspace user
  --timezone TIMEZONE   IANA timezone, e.g. America/Chicago
  --daily-dir DAILY_DIR
  --worktrees WORKTREES
  --agent NAME:task|standing
                        worker type and lifetime; repeat for multiple types
  --launcher {ghostty,tmux}
  --worker-mode {interactive,one-shot}
                        default dispatch mode written to [workers] in the workspace TOML
  --board-port BOARD_PORT
                        configure local board serving on this fixed port
  --github-repo GITHUB_REPO
                        GitHub owner/repository
  --gitlab-host GITLAB_HOST
                        GitLab https URL; also pass --gitlab-project
  --gitlab-project GITLAB_PROJECT
                        GitLab group/repository
  --gitlab-token-env GITLAB_TOKEN_ENV
  --dry-run             print the new block and settings without writing
```

### `work`

```text
usage: work.py [-h] [--root ROOT] [--body BODY] [--issue ISSUE] [--answer-ask ANSWER_ASK]
               [--answer ANSWER] [--recover] [--commit]
               title

Start work under a forge issue, filing it first when needed; preview unless --commit.

positional arguments:
  title                 title of the issue to file

options:
  -h, --help            show this help message and exit
  --root ROOT           workspace holding CLAUDE.md and store.db
  --body BODY           body of a new issue
  --issue ISSUE         start under an existing issue instead of filing one
  --answer-ask ANSWER_ASK
                        answer this stored ask on --issue (operator action)
  --answer ANSWER       answer to record for --answer-ask
  --recover             explicitly acknowledge failures on --issue and permit another attempt
  --commit              file the issue and write its store row
```

### `backlog`

```text
usage: backlog.py [-h] [--config CONFIG] [--list] [--state {opened,closed,all}] [--labels LABELS]
                  [--timeout TIMEOUT] [--create TITLE] [--body BODY] [--parent PARENT]
                  [--blocked-by IID] [--close ISSUE] [--comment IID] [--view N] [--comments]
                  [--commit]

Read the GitLab or GitHub backlog named in a ## Coordinator block.

options:
  -h, --help            show this help message and exit
  --config CONFIG       workspace CLAUDE.md holding the block
  --list                one line per issue instead of a count
  --state {opened,closed,all}
                        which issues
  --labels LABELS       comma-separated labels to narrow the ask
  --timeout TIMEOUT     seconds per request
  --create TITLE        file one issue
  --body BODY           the issue body, or the comment's text
  --parent PARENT       GitHub parent issue number for --create
  --blocked-by IID      GitHub blocker issue number for --create; repeat for multiple blockers
  --close ISSUE         close one issue: N, #N, or a reference in the Backlog
  --comment IID         comment on one issue
  --view N              print one issue's title, state, body and labels (#63)
  --comments            with --view, add the issue's comments in order
  --commit              actually write. Without it every write is printed and not made.
```

## Forge

### `kanban`

```text
usage: kanban_cli.py [-h] --root ROOT [--date DATE] [--issue ISSUE] [--task TASK]
                     [--from-stage FROM_STAGE] [--initialize] [--commit]

Preview or sync a tracked issue's Kanban stage without replacing unrelated labels.

options:
  -h, --help            show this help message and exit
  --root ROOT           workspace holding CLAUDE.md and the tracker
  --date DATE           tracker date, YYYY-MM-DD; defaults to today in the workspace timezone
  --issue ISSUE         one tracked issue reference, required with --commit
  --task TASK           with --issue and several rows on it: the tracker row's name or item number
                        (#69)
  --from-stage FROM_STAGE
                        stage last observed on the issue or project
  --initialize          start an unlabeled issue or project item
  --commit              apply one verified move; default is preview
```

### `pipeline`

```text
usage: pipeline_cli.py [-h] --root ROOT (--id ID | --ref REF)

Read a pipeline's status or branch CI from the configured forge. For GitHub branches, status is an
aggregate of every workflow's latest run at the head; ID and URL identify the newest run, whose
status may differ from the aggregate. GitLab branches report the latest pipeline at the head.

options:
  -h, --help   show this help message and exit
  --root ROOT  workspace holding CLAUDE.md and its Backlog: line
  --id ID      GitLab pipeline ID or GitHub Actions run ID
  --ref REF    branch head and CI: GitHub aggregates every workflow; ID and URL identify the
               newest run; GitLab reads the latest pipeline
```

## Observability

### `health`

```text
usage: probe_health.py [-h] [--config CONFIG] [--timeout TIMEOUT]

Check settings, runtimes and Health: targets; advise about a missing settings file (exit code
unaffected).

options:
  -h, --help         show this help message and exit
  --config CONFIG    workspace CLAUDE.md holding the block
  --timeout TIMEOUT  seconds per target
```

### `pages`

```text
usage: pages.py [-h] [--ensure] [--root ROOT] [--dir DIR] [--port PORT]
                [{serve,supervise,install,uninstall,status}]

Serve the workspace's pages locally: the board and any page a session writes beside it.

positional arguments:
  {serve,supervise,install,uninstall,status}

options:
  -h, --help            show this help message and exit
  --ensure              start the server unless it is serving; print status only
  --root ROOT           workspace root holding CLAUDE.md (default: the current directory)
  --dir DIR
  --port PORT
```

### `decisions`

```text
usage: decisions_cli.py [-h] [--root ROOT] [--json]
                        {leaves,add,reject,read,pending,judge,decide,grants,precedent} ...

Coordinator, architect and owner CLI for the decision graph and history verdicts (§17, #1181).

positional arguments:
  {leaves,add,reject,read,pending,judge,decide,grants,precedent}
    leaves              list current unreplaced decision leaves
    add                 append a new decision leaf to the graph
    reject              record a rejected alternative on an ancestor decision
    read                free-reader graph traversal (coordinator, architect or owner)
    pending             list pending worker history requests (justification text only, no history)
    judge               judge pending worker history requests without reading history
    decide              record an explicit coordinator verdict on a pending request
    grants              list logged decision history grants
    precedent           query precedent behind the coordinator's PrecedentSource seam

options:
  -h, --help            show this help message and exit
  --root ROOT           workspace root holding store.db
  --json                emit JSON instead of TOON
```

### `performance`

```text
usage: performance_report.py [-h] [--root ROOT] --since DATE [--until DATE] [--by {day,week}]
                             [--raw]

Print the agent performance table from the store's stage segments (architecture §9).

options:
  -h, --help       show this help message and exit
  --root ROOT      workspace root holding store.db and CLAUDE.md
  --since DATE     inclusive YYYY-MM-DD in the workspace zone
  --until DATE     inclusive YYYY-MM-DD; default today in the workspace zone
  --by {day,week}
  --raw            JSON attempt records, preserving outcomes and individual durations
```

### `limits`

```text
usage: chief-of-stuff limits [-h] [--root ROOT]

Take and record one unbilled reading of every runtime's account limits.

Use this to inspect locally observed quota before dispatch. These are readings from
local session transcripts and logs, not a live provider query.

options:
  -h, --help   flag, default: off. Print this help text on stdout and exit 0 without collecting or
               recording.
  --root ROOT  filesystem path, default: . (current working directory). Selects the workspace
               whose CLAUDE.md and named Settings TOML determine the launch account and [limits]
               warn_at.

When not to use:
  For workspace settings, runtime PATH, Health: target probes, and (advice) lines,
  use chief-of-stuff health. limits is not a connectivity or configuration health
  check and does not probe HTTP targets.

Output:
  Plain text on stdout (not TOON and not JSON). Normal completion leaves stderr
  empty. Command failures print "limits: <reason>" on stderr. Argparse usage
  faults print usage and errors on stderr. Help is text on stdout.

  After a header row, the command prints one line per limit reading. One runtime
  can yield multiple rows. Unknown readings keep exit code 0; the reason appears
  inline in the used/cap column as "unknown: <reason>", never as zero usage.

  Columns (space-separated):
  - runtime: host identifier (claude, codex, cursor, or agy).
  - account: profile or login name selected for that runtime; the internally empty
    default login is printed as default.
  - limit: window, model, or cap identifier from the local source, or limits when
    unavailable.
  - used/cap: numeric usage and cap with unit, e.g. 85/100%; or unknown: <reason>.
  - reset: reset time in ISO 8601, or unknown when absent.
  - observed: observation time in ISO 8601 (read_at). A known local event keeps its
    event time, so rerunning does not make old evidence fresh.

Exit codes:
  0  Readings recorded and printed, including when some or all are unknown; also
     successful -h/--help. Stderr is empty.
  1  Command failure such as unreadable or invalid settings, or shared-history
     creation, migration, or write failure; diagnosed on stderr as limits: <reason>.
  2  Argparse usage fault (unknown option, missing --root value, etc.); usage on
     stderr. Quota usage itself is never a nonzero status.

Side effects:
  No preview, dry run, or --commit gate: a normal invocation records immediately.
  Help performs no collection and writes no history.

  Reads: the workspace CLAUDE.md under --root; the Settings TOML named there when
  present (missing configuration uses defaults); local session logs for the launch
  account through session_logs.py. Claude reads structured events under
  $CLAUDE_CONFIG_DIR or ~/.claude, or $CLAUDE_AS_PROFILES_DIR/<profile> or
  ~/.claude-profiles/<profile> when [workers] claude_as_profile selects a profile.
  Other runtimes use their default login. This pass reads each runtime once under
  that account, not every account on the machine.

  Writes: creates, migrates, and updates the account-wide SQLite history at
  $XDG_STATE_HOME/chief-of-stuff/limits.sqlite (default
  ~/.local/state/chief-of-stuff/limits.sqlite). --root chooses account and
  configuration, not a workspace-local history file. The database is separate
  from the issue store. Repeated identical observations and unchanged unknown
  reasons may be deduplicated; recording does not promise a new row every time.

  Does not touch the issue store or forge, launch runtimes, or make billed model
  calls. The installed chief-of-stuff router may bootstrap its dependency venv
  before execing this script; that is separate from collection.

  [limits] warn_at in the Settings TOML is a finite fraction from 0 through 1,
  default 0.8. Downstream near-cap judgments compare observed used/cap or
  projected usage at reset with this threshold. This command validates settings,
  records, and prints readings; it does not print a threshold verdict, enforce a
  cap, or change exit status when usage is high. Unknown readings cannot
  establish a near-cap warning. Currently codex, cursor, and agy report
  unknown: read in 1.1 when no local numeric evidence exists.

Examples:
  chief-of-stuff limits --help
  chief-of-stuff limits
  chief-of-stuff limits --root /tmp/cedar-workspace
```

### `architect-signals`

```text
usage: architect_signals_cli.py [-h] [--root ROOT] {pending,mark,key,body,file} ...

Unassigned compliance gaps become forge issues once; the coordinator ledger never edits
compliance.md.

positional arguments:
  {pending,mark,key,body,file}
    pending             list unassigned open gaps not yet in the ledger
    mark                record a gap id as acted on (does not edit compliance.md)
    key                 print the signal key for a gap id and commit
    body                print an issue body for a pending gap
    file                dedupe, file and record under the coordinator's ledger lock

options:
  -h, --help            show this help message and exit
  --root ROOT
```

### `docs`

```text
usage: chief-of-stuff docs [-h]

Generate the command reference from each command's own argparse help.

options:
  -h, --help  show this help message and exit

When not to use:
  For one command's options, run chief-of-stuff <command> --help.

Output:
  The complete Markdown command reference on stdout; errors on stderr.

Exit codes:
  0: reference generated; 1: a command's help failed; 2: invalid arguments.

Side effects:
  Runs each visible command with --help. Writes no files itself.

Examples:
  chief-of-stuff docs
  chief-of-stuff docs > docs/commands.md
```
