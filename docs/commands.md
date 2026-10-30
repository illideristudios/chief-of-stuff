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
usage: chief-of-stuff init [-h] --root PATH --user NAME --timezone ZONE [--daily-dir DIR]
                           [--worktrees DIR] [--agent NAME:task|standing]
                           [--launcher {ghostty,tmux}] [--worker-mode {interactive,one-shot}]
                           [--board-port PORT] [--github-repo OWNER/NAME | --gitlab-host URL]
                           [--gitlab-project GROUP/NAME] [--gitlab-token-env NAME] [--dry-run]

Bootstrap configuration inside an existing workspace that has no Coordinator block.

Run it once on a directory that already exists and has no ## Coordinator block.

options:
  -h, --help            Boolean, default false. Print this text on stdout and exit 0. Other
                        arguments are not required.
  --root PATH           Path, required, no default. An existing workspace directory. init does not
                        create it.
  --user NAME           String, required, no default. Coordinator user: one trimmed, nonempty
                        printable line without backticks or pipes.
  --timezone ZONE       IANA timezone string, required, no default. Must be a zone ZoneInfo
                        recognizes, for example America/Chicago.
  --daily-dir DIR       Relative directory string, default daily/. Daily log and tracker paths,
                        kept inside the workspace with a trailing slash. The directory is not
                        created.
  --worktrees DIR       Relative directory string, default worktrees/. Coordinator worktree
                        location, with the same path rules. Worktrees are not created.
  --agent NAME:task|standing
                        Repeatable string, default none. Append one Agent line. The form is
                        NAME:task or NAME:standing; NAME matches [A-Za-z0-9][A-Za-z0-9._-]{0,63}.
  --launcher {ghostty,tmux}
                        Choice ghostty or tmux, default ghostty. Sets [workers].launcher only in
                        settings this run creates. Starts no terminal.
  --worker-mode {interactive,one-shot}
                        Choice interactive or one-shot, default interactive. Sets [workers].mode
                        only in settings this run creates. Dispatches no worker.
  --board-port PORT     Integer, default unset. Record a Pages board at http://127.0.0.1:PORT/
                        with directory pages/. Valid range 1-65535. Nothing is served or opened.
  --github-repo OWNER/NAME
                        owner/repository string, default unset. Select a GitHub backlog, mutually
                        exclusive with --gitlab-host. GitHub calls use gh. Required unless
                        --gitlab-host is set.
  --gitlab-host URL     HTTP or HTTPS URL string, default unset. Select a GitLab backlog, mutually
                        exclusive with --github-repo. Requires --gitlab-project. Required unless
                        --github-repo is set.
  --gitlab-project GROUP/NAME
                        Group/project string, default unset. GitLab project path. Requires
                        --gitlab-host, and is refused without it even when --github-repo is set.
  --gitlab-token-env NAME
                        Environment-variable name, default CHIEF_OF_STUFF_GITLAB_TOKEN. Recorded
                        on a GitLab Backlog line and used for GitLab calls. The token value is
                        never written. Checked even for a GitHub backlog.
  --dry-run             Boolean, default false. Validate and print the intended file changes and
                        label intent. No file writes and no forge calls.

When not to use:
  A workspace that already has a ## Coordinator block is refused. To install or
  keep model guidance on a workspace that is already configured, use
  chief-of-stuff models --root PATH. Init is not an update and not a reset.
  Parent [workspaces] setup (architecture section 12, issue #825) is not
  implemented: init does not read or write that table, register child
  workspaces, or start an engine.

Output:
  Human-readable text. Not TOON and not JSON, and there are no structured
  fields. --help writes this text on stdout and leaves stderr empty.
  A successful write prints, on stdout, an optional "ignored in EXCLUDE" line,
  then optional "hold label" and "stage label" lines for labels the forge
  created, then "configured ROOT; settings in SETTINGS; model guidance in
  GUIDANCE". ROOT, SETTINGS and GUIDANCE are the workspace path and the two
  destination paths.
  --dry-run prints "would append to" or "would create" for the Coordinator
  file and prints the proposed block; "would keep existing" or "would create"
  for settings, and prints new settings only when the file is absent; the same
  keep-or-create line for model guidance, without its contents; and optional
  "would create the hold label" or "would create the stage label" lines, or a
  "would check GitHub Project" line when existing settings name a project.
  A forge that refuses a label prints "hold label not created" or "stage label
  not created" on stderr, and initialization still continues. Configuration and I/O refusals print
  "refused:" on stderr. Usage faults print usage and an error on stderr.

Exit codes:
  0  initialization, --dry-run, or --help succeeded. A label warning on stderr
     is still exit 0.
  1  refused configuration or I/O, including a missing GitLab partner flag, a
     --board-port outside 1-65535, a bad user, timezone, directory, or token
     variable, an existing Coordinator block, and invalid existing settings.
  2  usage fault: a missing required argument, an unknown option, an invalid
     choice, a non-integer --board-port, or both --github-repo and --gitlab-host.

Side effects:
  Reads the workspace directory, an existing Coordinator file, existing
  settings (validated, not rewritten), the shipped model-guidance template
  when guidance is created, and timezone data. Inside a git work tree it also
  reads the exclude file. A real run with a forge reads that forge and may
  create missing labels. --dry-run reads the same local files and does not
  contact the forge.
  Appends one ## Coordinator block to an existing file, keeping the text above
  it and trimming trailing whitespace. Creates the file under a "# Workspace"
  heading when it is absent. An existing Coordinator block is never replaced.
  Creates the settings file only when absent: notifications off, the chosen
  launcher and worker mode, a [kanban] map for the shipped workflow, hold label
  "needs human", and commented examples. Existing valid settings are kept.
  --launcher and --worker-mode change only settings this run creates.
  Creates model guidance only when absent and keeps an existing file.
  Inside a git work tree, appends ignore rules for the new files, the daily and
  worktree directories, and engine state names (store.db and .chief-of-stuff/).
  A Coordinator file is listed only when this run creates it. Other exclude
  lines stay.
  It does not create a store, issues, a repository, daily files, worktrees,
  board files, or a running service, and it does not install or start an
  engine or supervisor, launch a worker, or open a browser. --board-port only
  records the board URL and pages/ directory.
  A forge choice is required. GitHub calls use gh authentication. GitLab
  records the token variable name and uses it for API calls; the token value
  is never written. New settings cause the hold label and one stage label per
  shipped workflow stage to be created when the forge lacks them. Existing
  settings that name a GitHub Project are checked for those options instead of
  creating stage labels; a missing hold label is still created. Those forge
  writes happen immediately. There is no --commit flag. --dry-run only reports
  them. There is no rollback: label provisioning runs before the local writes,
  and a later I/O failure can leave a partial workspace.

Examples:
  The --root directory must already exist: run mkdir -p ROOT first.
  chief-of-stuff init --dry-run --root /tmp/alder --user Robin --timezone America/Chicago --github-repo example-team/alder
  chief-of-stuff init --root /tmp/birch --user Morgan --timezone America/Chicago --github-repo example-team/birch --agent implementer:task --launcher tmux
  chief-of-stuff init --root /tmp/cedar --user Casey --timezone America/Chicago --gitlab-host https://gitlab.example.test --gitlab-project example-team/cedar --gitlab-token-env CEDAR_GITLAB_TOKEN
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
usage: limits_cli.py [-h] [--root ROOT]

Take and record one unbilled reading of every runtime's account limits.

options:
  -h, --help   show this help message and exit
  --root ROOT
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
