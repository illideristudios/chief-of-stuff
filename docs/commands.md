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
usage: chief-of-stuff pages [-h] [--ensure] [--root ROOT] [--dir DIR] [--port PORT]
                            [{serve,supervise,install,uninstall,status}]

Serve the workspace's pages locally: the board and any page a session writes beside it.

The pages are routes: `/` the board, `/workers` the running workers and today's ends, `/decisions` the list,
`/decisions/<slug>` an authored decision page, `/decisions/<id>` (digits only) a store decision's page rendered
from the store (#945), `/issues/<n>` a tracker issue's page, `/issues/<n>/source` its change's source
and diff. `/workers/` and `/decisions/` are the same pages as the bare routes (#988); other directory URLs stay 404
(#340). A missing `/favicon.ico` answers 204 so the browser console stays clean; a real icon in the pages dir is
served. Each is rendered into its file (`<day>-board.html`, `workers.html`, `decisions.html`, `decision-<slug>.html`,
`issue-<n>.html`, `issue-<n>-source.html`), which only the routes serve: a request for the file's own name is a 404.
A GET re-renders a page when one of its sources (the trackers, CLAUDE.md, the settings TOML, the decision JSON,
`.sources.json`) is newer than its last render, or differs in bytes from what that render read, and `/workers` on every GET (it reads the session logs too), and a thread refreshes `.sources.json` from the forge every minute. An open tab
reloads itself when the file changes.

    chief-of-stuff pages [--ensure] [--root R]            # start it unless it is already serving; print status only
    chief-of-stuff pages serve --dir D --port P --root R  # what --ensure starts, detached
    chief-of-stuff pages install|uninstall|status [--root R]  # the independent board LaunchAgent (#935)
    chief-of-stuff pages supervise [--root R]             # what that LaunchAgent runs: ensure, every POLL seconds
    chief-of-stuff pages --board [--date D] [--root R]    # what `chief-of-stuff board` runs: write the board file, no server

A decision page posts the user's answer to `POST /decisions/<slug>` (`key` and/or `words`, form-encoded).
The server saves it as the decision JSON's `answer` ({key, words, at}) for the coordinator's next pass, and
redirects back to the page, which then shows the decision answered.

The `## Coordinator` block's `Board:` line names both halves: `URL http://127.0.0.1:<port>/` and
``dir `<pages dir>` ``. It listens on 127.0.0.1 only, and it answers only requests whose Host is
127.0.0.1 or localhost. `/` is today's board once today's tracker exists, else the newest one; `/all` lists every other file a session
wrote there. Binding to loopback does not stop a page elsewhere from rebinding its own
name to 127.0.0.1, but that page still sends its own name as the Host.

positional arguments:
  {serve,supervise,install,uninstall,status}
                        string; default omitted (ensure); serve is the internal foreground server,
                        supervise the LaunchAgent's loop, install|uninstall|status manage that
                        LaunchAgent

options:
  -h, --help            show this help message and exit
  --ensure              flag; default off; ensure the server (also the bare command's behavior)
  --root ROOT           path; default . when ensuring, none with serve; workspace holding
                        CLAUDE.md
  --dir DIR             path; default none; required with serve, ignored otherwise; pages
                        directory
  --port PORT           integer; default none; required with serve, ignored otherwise; listen port

When not to use:
  To open the board for the user, use a browser: nothing here ever opens it. To write the board file without a
  server, use chief-of-stuff board. To write a decision page, use chief-of-stuff decision. Agents do not run
  serve: --ensure starts it detached.

Output:
  Text, not TOON or JSON, except status (a TOON record). Ensuring prints exactly one stdout status line, never
  the URL: pages: serving (the same or a newer version already answers), pages: started, or pages: restarted
  (an older pages server was replaced). install and uninstall print one line saying what changed. status prints
  the board and the engine separately: board state, port, pid, whether the LaunchAgent exists, whether a
  supervisor holds the board. serve and supervise print no status line; a detached serve appends its stdout and
  stderr to <root>/.chief-of-stuff/pages.log. Failures print pages: not served - <reason> (ensuring) or
  pages: <reason> (install, uninstall, status) on stderr; usage faults print usage and an error on stderr.

Exit codes:
  0  serving, started or restarted; install, uninstall or status succeeded; help printed; serve and
     supervise run until stopped (SIGTERM or SIGINT end supervise with 0).
  1  ensuring failed: missing Board line or unreadable config, a foreign server owns the port, an older server
     cannot be identified or stopped, the new server did not come up, or a connection was denied (often a sandbox).
  2  usage fault (unknown argument, serve without --dir and --port, --port not an integer), or install,
     uninstall or status failed (pages: <reason> on stderr).

Side effects:
  Ensuring reads the Board line, probes HEAD http://127.0.0.1:<port>/all and, for an older server, reads
  <pages dir>/.pid and checks that process's command line. It creates the pages and log directories, appends
  <root>/.chief-of-stuff/pages.log, writes .pid only after a new server answers, and may SIGTERM an older pages
  serve on that port. It never switches to another port.
  install needs macOS and a Board line; it writes the board LaunchAgent under the supervisor lock and loads it; uninstall removes the agent and
  leaves a running page server serving until it stops or the Mac restarts; status writes nothing. supervise
  checks the server every few seconds and restarts it (what the LaunchAgent runs).
  The server binds only to 127.0.0.1 and accepts only a 127.0.0.1 or localhost Host. Routes: / is today's board
  once today's tracker exists, else the newest board; /workers (and /workers/) the running workers and today's
  ends; /decisions (and /decisions/) the list; /decisions/<slug> an authored decision; /decisions/<id> (digits)
  a store decision rendered from the store; /issues/<n> and /issues/<n>/source; /all every other non-hidden
  file a session wrote there. Rendered HTML files are served only through their routes, not their filenames.
  A GET (and HEAD) re-renders a page when its sources change in time or bytes; /workers renders on every GET.
  Rendering writes HTML into the pages directory, keeping unchanged files; an open tab reloads itself. A render
  failure keeps the last good page with an error banner. A background thread reads forge and worker facts at
  start and about every 60 seconds and writes <pages dir>/.sources.json.
  POST /decisions/<slug> (form: key and/or words) writes that decision JSON's answer {key, words, at}. POST / (a
  store decision's form) records an answer, reversal or re-route as an intent row that the engine alone
  applies: the request never touches the forge or a card. POST /workers/sessions/<n>/messages records a message
  for that worker session in the store (same-origin form only). Only this machine's own pages may post.
  No forge writes and no --commit, preview or dry run: writes happen as the server runs.

Examples:
  chief-of-stuff pages --ensure --root ~/work/field-notes
  chief-of-stuff pages status --root ~/work/field-notes
  chief-of-stuff pages install --root ~/work/field-notes
  chief-of-stuff pages --help
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
