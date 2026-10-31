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

### `merge-order`

```text
usage: merge_order.py [-h] --root ROOT [--repo REPO]

Propose a least-conflict order for open changes using pairwise trial merges.

options:
  -h, --help   show this help message and exit
  --root ROOT  workspace holding CLAUDE.md and the git checkout
  --repo REPO  owner/name or group/name; default the backlog's

Pairwise clean results do not guarantee a clean sequential merge.
```

## Observability

### `health`

```text
usage: chief-of-stuff health [-h] [--config CONFIG] [--timeout TIMEOUT]

On open or resume, check workspace settings, the Claude runtime, and Health: targets.

Use it when the question is that preflight: whether the Settings: file loads, whether
Claude is on the login PATH when a [models] rotation names it, whether workspace files
need (advice), and whether each Health: URL answers. A non-zero exit with empty stderr is
the count of failed checks, which an agent can branch on without parsing.

options:
  -h, --help         flag, default: off. Print this help text on stdout and exit 0.
  --config CONFIG    path, default: CLAUDE.md. File whose ## Coordinator block is read; its parent
                     is the workspace root for the Settings: path and for (advice) commands.
  --timeout TIMEOUT  float seconds, default: 3.0. Per-target HTTP GET timeout only; does not bound
                     the settings load or the Claude login-PATH lookup (resolve uses 15 seconds).

When not to use:
  To see which worker processes are alive, use chief-of-stuff processes
  (processes); health does not read PIDs.
  To create a file that an (advice) line names, use chief-of-stuff init
  (init); health prints the command and does not run it.
  To audit tasks against worktrees and issues, use chief-of-stuff audit
  (audit).

Output:
  Plain text lines on stdout (not TOON and not JSON; no structured fields).
  Stderr is empty unless the exit is a usage fault (exit 2). Lines are emitted
  on stdout in this order when present:

  1. Settings probe (only when a top-level "- Settings:" bullet in
     ## Coordinator names a path; a repeated key's last bullet wins; without
     that bullet no settings line is printed and the file is not read):
     - "settings <name> ok" when the file loads cleanly.
     - "settings <name> absent, defaults" when the named file is not there.
     - "settings <name> invalid: <why>" when the file cannot be read or fails
       TOML/schema validation (<why> is one line, names <name> once, and carries
       no machine-specific absolute path).

  2. Claude runtime probe (only when the settings file loaded and a [models]
     rotation names claude; checked once via claude_adapter.available() on the
     login shell PATH; only claude is checked; codex, cursor, and agy are not
     probed):
     - "runtimes claude ok" when claude is found on the login PATH.
     - "runtimes claude missing" when claude is not found.
     - "runtimes not checked: <why>" when the login shell fails (redacts
       directory prefixes from paths and says neither ok nor missing).
     No line is emitted when claude is not named or when settings did not load.

  3. Workspace file advice lines (zero or more, from WORKSPACE_FILES, printed
     whether or not ## Coordinator has a Settings: line, after any settings and
     runtimes lines and before targets):
     - "workspace file <name> missing (advice); <command>"
     - "workspace file <name> not a file (advice); move it aside, then <command>"
       (directory, broken symlink, fifo, or other non-regular file)
     - "workspace file <name> unreadable (advice); <why>"
     Each advice line starts with "workspace file <name>" and contains
     "(advice)". Advice lines are not failed checks: an (advice) line does not
     change the exit code. <name> is the configured Settings: path, or
     chief-of-stuff.toml when ## Coordinator names none; a custom path that is a
     regular file does not also advise the default name. <command> has {root}
     replaced by the shell-quoted resolved workspace directory (currently
     "chief-of-stuff init --help"). Model guidance is never advised and health
     does not install it. A missing --config file is a usage fault on stderr,
     not an advice line. A Settings: name with a NUL (ValueError) gets the
     settings invalid line and no duplicate advice line.

     Advice against failed checks and absent, defaults:
     - "settings <name> absent, defaults" is neither a failed check nor an
       advice line; nothing was validated, defaults apply, and it never says ok.
     - Missing configured settings file: "settings <name> absent, defaults" (0)
       plus "workspace file <name> missing (advice); ..." (0) -> 0 failed checks.
     - Directory or non-regular file at the Settings: path: "settings <name>
       invalid: ..." (1) plus "workspace file <name> not a file (advice); ..."
       (0) -> 1 failed check.
     - Unreadable Settings: path: "settings <name> invalid: ..." (1) plus
       "workspace file <name> unreadable (advice); <why>" (0) -> 1 failed check.

  4. Health: target probes (each "- Health: <name> <url> [status]" line inside
     ## Coordinator, in block order, or "no Health: targets in the
     ## Coordinator block"):
     - "<name> <url> <status> ok" when the HTTP GET status equals the expected
       status (default 200).
     - "<name> <url> <status>, expected <expect>" on any other HTTP status,
       including 4xx/5xx responses.
     - "<name> <url> unreachable (<ExceptionType>: <detail>)" when the request
       fails without an HTTP status (timeout, connection refused, DNS, TLS).
     Every target gets a line even when it fails. Probe requests send GET with
     "User-Agent: chief-of-stuff/probe"; the printed <url> strips query strings
     and credentials. Only http and https URLs are valid targets; a malformed
     Health: line inside ## Coordinator prints to stderr and exits 2 after steps
     1-3 have already printed to stdout.

Exit codes:
  0  No failed checks (or -h/--help). Includes settings ok, settings absent
     (absent, defaults), claude ok, claude not named, no Health: targets, all
     targets ok, and any (advice) lines. Stderr is empty.
  N  (1 or more, with empty stderr) N failed checks: +1 if settings are invalid
     or unreadable; +1 for "runtimes claude missing" or "runtimes not checked"
     (however many rotations named claude); +1 per target that is not ok.
     Advice lines and absent, defaults add 0. Exit 2 with empty stderr means two
     failed checks, not a usage fault.
  2  (with non-empty stderr) Usage fault. Any failed-check count already
     computed is discarded. Causes: argparse error (unknown flag, non-float
     --timeout); --config is not a file ("no config at <path>", stdout empty);
     --config is not valid UTF-8 ("<path>: not UTF-8 (<reason>)", stdout empty);
     a Health: line in ## Coordinator is not "<name> <http(s)-url> [int]"
     (stdout may already hold settings, runtimes, and advice lines).

Side effects:
  Reads only. No preview or --dry-run and no --commit exist, because nothing is
  written.

  Reads: the --config file; the settings file via the settings loader when
  Settings: names one; stat/lstat on WORKSPACE_FILES paths; Claude's binary on
  the login shell PATH when a [models] rotation names claude; one HTTP/HTTPS GET
  per Health: target.

  Writes: none. Not the store, not the forge, not the workspace files, not the
  probed services. Advice does not run init.

Examples:
  chief-of-stuff health --config ~/cedar-desk/CLAUDE.md
  chief-of-stuff health --config ~/cedar-desk/CLAUDE.md --timeout 1
  chief-of-stuff health
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
usage: chief-of-stuff performance [-h] [--root ROOT] --since DATE [--until DATE] [--by {day,week}]
                                  [--raw]

Print the agent performance table from the store's stage segments (architecture §9).

Use this to aggregate one-shot worker run counts, outcomes, and median duration over
a date window by day or week and by runtime, model, effort, and agent type.

options:
  -h, --help       flag, default: off. Print this help text on stdout and exit 0 without reading
                   CLAUDE.md or opening store.db.
  --root ROOT      filesystem path, default: . (current working directory). Selects the workspace
                   root holding CLAUDE.md (for the Coordinator timezone) and store.db.
  --since DATE     ISO date YYYY-MM-DD, default: none (required). Sets the inclusive start date of
                   the reporting window in the workspace timezone, matched against each segment's
                   started_at date.
  --until DATE     ISO date YYYY-MM-DD, default: today in the workspace timezone. Sets the
                   inclusive end date of the reporting window in the workspace timezone, matched
                   against each segment's started_at date.
  --by {day,week}  grouping period {day,week}, default: day. Groups segments into daily buckets
                   (period is the segment's start date) or Monday-anchored weekly buckets (period
                   is that week's Monday).
  --raw            flag, default: off. Print JSON attempt records, preserving each outcome and
                   individual duration, instead of the grouped table.

When not to use:
  For live board, worker, or decision pages, use chief-of-stuff board or
  chief-of-stuff pages. To read a day's human-readable tracker prose or sections,
  use chief-of-stuff tracker. For runtime account quota readings, use
  chief-of-stuff limits; for workspace settings, runtime PATH, and Health: target
  probes, use chief-of-stuff health.

  This command reads persisted store stage segments only (store.db via
  store_access.migrated_store). Daily tracker ## Log lines, one-shot report files,
  and interactive sessions are not sources (architecture §9).

Output:
  Without --raw, a plain text table on stdout (two-space-separated, left-padded
  columns; not TOON and not JSON). With --raw, one JSON array on stdout of attempt
  records (one per stage segment started in the window, in the workspace
  timezone, with its outcome and individual duration) instead of the table; the
  columns and grouping below do not apply. Normal completion prints the header row followed by zero or more
  sorted data rows on stdout (an empty window prints the header row only) and
  leaves stderr empty. Help prints text on stdout with stderr empty. Workspace,
  date, and missing-store errors print "performance: <reason>" on stderr with
  stdout empty. Argparse usage faults print usage and errors on stderr.

  Windowing, grouping, and keys (§9):
  Each segment's started_at timestamp is converted to the workspace timezone
  (Timezone: in CLAUDE.md). Segments whose start date falls in [--since, --until]
  inclusive are grouped by (period, runtime, model, effort, agent_type, workflow) sorted
  ascending; a run that ends after midnight stays in the period of its start.
  With --by day, period is the start date (YYYY-MM-DD); with --by week, a week
  starts on Monday and period is that week's Monday (YYYY-MM-DD). A segment with
  no runtime, model, effort, agent_type, or workflow is keyed by what is recorded, and an
  empty key field prints as "-".

  Columns (in order):
  - period: first day of the bucket (YYYY-MM-DD): the start day itself (--by day)
    or that week's Monday (--by week).
  - runtime: host runtime identifier recorded on the segment (claude, codex,
    cursor, or agy), or "-" when empty.
  - model: model identifier recorded on the segment, or "-" when empty.
  - effort: reasoning effort recorded on the segment, or "-" when empty.
  - agent_type: declared workspace agent type recorded on the segment, or "-"
    when empty.
  - workflow: workflow recorded on the segment, or "-" when empty (an older
    segment's unknown workflow stays empty).
  - runs: total stage segments started in the bucket for that key, including
    open (unended) segments.
  - completed: segments with outcome == "done".
  - human_review: segments with outcome == "human_review" or
    required_human_review set.
  - relaunched: segments with attempt > 1.
  - median_minutes: median whole minutes (int((ended_at - started_at) // 60s))
    across ended segments in the group, taking the lower middle value when the
    count is even, or "-" when no segment in the group has ended. Open segments
    count in runs but not in median_minutes.

Exit codes:
  0  Table printed on stdout (including when no segments match the window), or
     successful -h/--help. Stderr is empty.
  1  Refusal from the top-level chief-of-stuff router before launching the script
     if the store-backed engine virtualenv cannot be prepared ("refused: <reason>"
     on stderr), or an uncaught store migration/lock ServiceError (for example,
     when store.db is at an older revision while a live engine holds engine.lock).
  2  Usage or workspace fault: argparse error (missing --since, invalid --by
     choice, unknown option), invalid --since or --until ISO date, unreadable or
     invalid CLAUDE.md / ## Coordinator block, or missing <root>/store.db
     ("performance: no store at <path>" on stderr).

Side effects:
  No preview or --dry-run mode and no --commit gate exist, because the command
  writes no application, tracker, or forge records. Help (-h/--help) performs no
  workspace read, lock acquisition, or store access.

  Reads: <root>/CLAUDE.md (## Coordinator block and Timezone) via
  workspace.read_config, and <root>/store.db stage segments (segment and resume
  tables) via store_access.migrated_store and db.segments().

  Writes and locks: does not write segment rows, work items, decisions, daily
  tracker files, board pages, or forge issues, and starts no service or model
  session. Opening store.db via store_access.migrated_store acquires
  <root>/.chief-of-stuff/migrate.lock; when store.db is already at the current
  Alembic head it only reads, and when store.db is at an older revision while no
  engine holds <root>/.chief-of-stuff/engine.lock it migrates store.db to head
  before reading. When invoked through the top-level chief-of-stuff performance
  router, the router acquires supervisor.lock in the chief-of-stuff data
  directory to ensure the store-backed virtualenv before execing this script.

Examples:
  # Daily rows from 2026-10-01 through today in the current workspace
  chief-of-stuff performance --since 2026-10-01

  # Daily rows from 2026-10-01 through today in an explicit workspace
  chief-of-stuff performance --root /tmp/maple-workspace --since 2026-10-01

  # Weekly Monday-anchored rows for a closed date range
  chief-of-stuff performance --root /tmp/maple-workspace --since 2026-10-01 --until 2026-10-07 --by week

  # JSON attempt records for the same range, ungrouped
  chief-of-stuff performance --root /tmp/maple-workspace --since 2026-10-01 --until 2026-10-07 --raw
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
