# Command reference

<!-- Generated from argparse help. Do not edit by hand.
Regenerate (also to resolve conflicts):
python3 chief_of_stuff.py docs > docs/commands.md
-->

## Engine

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

Add a minimal chief-of-stuff Coordinator block to a workspace.

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
usage: kanban.py [-h] --root ROOT [--date DATE] [--issue ISSUE] [--task TASK]
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

## Observability

### `board`

```text
usage: render_board.py [-h] [--date DATE] [--root ROOT]

Render a tracker-based daily board without opening a server or browser.

options:
  -h, --help   show this help message and exit
  --date DATE  YYYY-MM-DD; default: today in the workspace timezone
  --root ROOT  workspace root holding CLAUDE.md
```

### `health`

```text
usage: probe_health.py [-h] [--config CONFIG] [--timeout TIMEOUT]

Check the Settings: file and its runtimes, and probe the Health: targets in a ## Coordinator
block.

options:
  -h, --help         show this help message and exit
  --config CONFIG    workspace CLAUDE.md holding the block
  --timeout TIMEOUT  seconds per target
```

### `pages`

```text
usage: pages.py [-h] [--ensure] [--root ROOT] [--dir DIR] [--port PORT] [{serve}]

Serve the workspace's pages locally: the board and any page a session writes beside it.

positional arguments:
  {serve}

options:
  -h, --help   show this help message and exit
  --ensure     start the server unless it is serving; print status only
  --root ROOT  workspace root holding CLAUDE.md (default: the current directory)
  --dir DIR
  --port PORT
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
