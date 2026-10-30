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
usage: chief-of-stuff backlog [-h] [--config CONFIG] [--list] [--state {opened,closed,all}]
                              [--labels LABELS] [--timeout TIMEOUT] [--create TITLE] [--body BODY]
                              [--parent PARENT] [--blocked-by IID] [--close ISSUE] [--comment IID]
                              [--view N] [--comments] [--commit]

Read and write the configured GitHub or GitLab backlog.

Use it to count, list, view, create, comment on, or close issues in the forge named by the
Backlog: line of a workspace's ## Coordinator block.

options:
  -h, --help            flag, default: off. Print this help text on stdout and exit 0.
  --config CONFIG       path, default: CLAUDE.md. Workspace markdown file holding the ##
                        Coordinator block.
  --list                flag, default: off. Print one line per issue instead of open and closed
                        counts.
  --state {opened,closed,all}
                        choice (opened, closed, all), default: opened. Which issues --list
                        returns; ignored by counts and writes.
  --labels LABELS       comma-separated string, default: "" (none). Narrow --list to issues with
                        every label, or attach labels on --create.
  --timeout TIMEOUT     float seconds, default: 5.0. Per-request timeout for GitLab API calls
                        (GitHub gh calls use 60s).
  --create TITLE        string TITLE, default: none. File one issue with TITLE (and --body,
                        --labels, --parent, --blocked-by). Previews unless --commit.
  --body BODY           string, default: "". Issue description for --create or note text for
                        --comment.
  --parent PARENT       int issue number, default: none. GitHub parent issue number for --create;
                        refused on GitLab (exit 1).
  --blocked-by IID      repeatable int IID, default: [] (none). GitHub blocker issue number for
                        --create; repeat for multiple blockers; refused on GitLab (exit 1).
  --close ISSUE         issue reference (N, #N, owner/repo#N, or URL in this backlog), default:
                        none. Close one issue; previews unless --commit.
  --comment IID         int IID, default: none. Post --body as a comment on issue IID (positive
                        integer); previews unless --commit.
  --view N              int N, default: none. Print issue N's title, state, body, and labels
                        (#63). Do not combine with write flags.
  --comments            flag, default: off. With --view, append the issue's non-system comments in
                        chronological order.
  --commit              flag, default: off. Execute the write on the forge; without it every write
                        is previewed and not made.

When not to use:
  To start engine work under a new or existing issue and record its store row,
  use chief-of-stuff work.
  To preview or sync a tracked issue's Kanban stage or Project status, use
  chief-of-stuff kanban.
  To read forge pipeline status or branch CI, use chief-of-stuff pipeline.
  To read or write daily tracker rows, use chief-of-stuff tracker or
  chief-of-stuff log.
  To render or serve the HTML workspace board, use chief-of-stuff board or
  chief-of-stuff pages.

Output:
  Plain text only (not TOON and not JSON); there are no structured record fields.
  - Default count mode (no --list, --view, or write flags) prints one line on
    stdout: "backlog: OPEN open, CLOSED closed" when both counts succeed, or
    "backlog: unknown — REASON" on stdout when a count fails.
  - --list prints one "#IID TITLE [LABEL, ...]" line per matching issue on
    stdout (the bracketed label list is omitted when an issue has no labels).
    A failed or truncated list prints "backlog: unknown — REASON" on stderr
    and leaves stdout empty.
  - --view N prints "#N TITLE [LABEL, ...]", then "state: opened|closed", then
    the issue body on stdout. With --comments, each non-system comment follows
    in chronological order as "CREATED_AT AUTHOR: BODY". A failed view or
    comment read prints "backlog: unknown — REASON" on stderr and leaves stdout
    empty.
  - Write flags (--create, --close, --comment) print one line per write on
    stdout: "would create: TITLE", "would close: #IID", or
    "would comment: #IID" in preview mode; "created #IID: TITLE",
    "closed #IID", or "commented on #IID" when --commit succeeds; or
    "ACTION failed: TARGET — REASON" when a write fails. When --commit is
    omitted, stderr also prints
    "nothing was written: add --commit to make these real".
  - An unknown result is never an empty backlog and never prints "0 open".
  - Usage and configuration faults print to stderr and leave stdout empty.

Exit codes:
  0  Read succeeded, write preview succeeded (without --commit), or committed
     write (--commit) succeeded.
  1  Forge read or write failed, including a missing gh binary or GitLab token,
     HTTP or network failure, non-JSON response, truncated backlog or comment
     list (5000 or more GitHub issues, or more than 50 GitLab pages of 100
     items), or --parent / --blocked-by used against a GitLab backlog.
  2  usage fault or configuration fault, including an invalid CLI flag or
     argument type, a missing --config file, an absent or malformed Backlog:
     line in ## Coordinator, or --close / --view naming an issue outside the
     configured backlog.

Side effects:
  - Reads the markdown file at --config (default CLAUDE.md) and parses the
    Backlog: line in its ## Coordinator section:
    - GitHub: "- Backlog: GitHub issues; repo acme-co/ledger-demo" (or a full
      https://github.com/acme-co/ledger-demo URL). Invokes gh (or the absolute
      executable path in $CHIEF_OF_STUFF_GH) using gh authentication.
    - GitLab: "- Backlog: GitLab; host https://gitlab.example.test; project
      acme-co/ledger-demo; token env CHIEF_OF_STUFF_GITLAB_TOKEN". Reads the
      token from the named environment variable (default
      CHIEF_OF_STUFF_GITLAB_TOKEN), then macOS Keychain (account
      "chief-of-stuff", service "gitlab-HOST"), then "glab config get token
      --host HOST". Never accepts a token flag or a literal token in CLAUDE.md.
  - Preview by default: --create, --close, and --comment are dry-run previews
    unless --commit is passed. Without --commit, no issue, comment, state
    change, or label is written on the forge. A GitHub preview makes no gh
    call (so it does not check labels, --parent, or --blocked-by, and the
    preview line does not echo --parent or --blocked-by). A GitLab --create
    preview checks for a token and, when --labels is set, reads existing
    project labels without creating any.
  - With --commit, writes go to the forge only:
    - --create on GitLab creates any missing labels from --labels first (POST
      /api/v4/projects/PROJECT/labels, color #428bca) before creating the
      issue. On GitHub, --create passes --label, --parent, and --blocked-by to
      "gh issue create" (missing labels fail in gh).
    - --parent and --blocked-by attach native GitHub issue relationships on
      --create; GitLab refuses them with exit 1 in both preview and --commit
      modes.
    - --close closes the issue on the forge; --comment posts a note on the
      issue.
  - Writes no local files, never opens or modifies store.db or daily trackers,
    and starts or stops no services or workers.

Examples:
  chief-of-stuff backlog --list --state all --labels defect,export
  chief-of-stuff backlog --create "Add retry to the invoice export" --parent 12 --blocked-by 7
  chief-of-stuff backlog --create "Add retry to the invoice export" --body "Retry once on HTTP 502." --labels defect --parent 12 --blocked-by 7 --commit
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
usage: chief-of-stuff pipeline [-h] --root ROOT (--id ID | --ref REF)

Read one pipeline or one branch head's CI from the workspace Backlog: forge.

Use it when the question is that pipeline's status. A failed status is still a successful read.

options:
  -h, --help   flag, default: off. Print this help text on stdout and exit 0.
  --root ROOT  path, default: none (required). Workspace whose CLAUDE.md Backlog: line selects the
               forge and repository.
  --id ID      int, default: none; exactly one of --id or --ref is required. One GitLab pipeline
               or one GitHub Actions run. Mutually exclusive with --ref; must be > 0. Does not
               aggregate.
  --ref REF    string, default: none; exactly one of --id or --ref is required. Branch name.
               GitHub: status is the aggregate of every workflow's latest run at that head; ID and
               URL identify the newest run, whose own status may differ. GitLab: the latest
               pipeline at that head (one pipeline, so id, url, and status agree). Mutually
               exclusive with --id.

When not to use:
  To decide whether an open pull or merge request is ready to merge, use
  chief-of-stuff merge-ready. That verdict already includes the head pipeline
  plus conflicts, draft, review threads, and a stale pipeline sha. This command
  does not.

Output:
  TOON on stdout, one object, fields in this order: id (int, or null), ref
  (string), sha (string), status (string), url (string, "" when there is no
  pipeline). Stderr is empty on success.

  No pipeline (--ref and the head has no runs): exit 0, id: null, status: none,
  url: "". A missing --id is not this result; it is a read error.

  GitHub status, for the single --id run and for the --ref aggregate: passed
  when every included check completed with success, neutral, or skipped;
  failed when any included check completed with another conclusion; running when
  none failed and at least one has not completed. On --ref, a truncated workflow
  list is a read error, not a partial aggregate.

  For --ref on GitHub, status is the aggregate of every workflow's latest run at
  that head; ID and URL identify the newest run, whose own status may differ
  from the aggregate. For --ref on GitLab, the latest pipeline at that head sets
  id, url, and status together.

  GitLab status, for --id and for the latest --ref pipeline: success maps to
  passed; failed and canceled map to failed; skipped, pending, created,
  waiting_for_resource, waiting_for_callback, preparing, scheduled, manual,
  draft, and canceling stay as those words; any other word maps to running.

  Stderr: pipeline: … on a read or config error, and argparse usage text on a
  usage fault. A usage fault prints no TOON.

Exit codes:
  0  The read succeeded, including status: failed and status: none; --help.
  1  Config or forge read failed (no Backlog: line, missing GitLab credential,
     HTTP error, unreadable payload, truncated GitHub run list, branch name
     that is blank, untrimmed, ".", or ".."). Stdout empty.
  2  Usage fault: missing --root, missing both selectors, both --id and --ref,
     a non-integer --id, or --id that is not positive.

Side effects:
  Reads only. No dry run and no --commit, because nothing is written.

  Reads CLAUDE.md (the Backlog: line), then the forge. GitHub uses gh api.
  GitLab uses REST on that line's host, including a self-hosted host, with the
  credential backlog.token already resolves (not a flag). --id reads
  actions/runs/<id> or pipelines/<id>. --ref reads the branch, then that head's
  runs.

  Writes: none. Not the store, not the board cache, not the worktree, not the
  forge.

Examples:
  chief-of-stuff pipeline --root ~/cedar-desk --id 71
  chief-of-stuff pipeline --root ~/cedar-desk --ref main
  chief-of-stuff pipeline --root ~/cedar-desk --ref release/ledger
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
