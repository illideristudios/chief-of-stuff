# Backlog setup

The workspace `CLAUDE.md` `## Coordinator` block may name one issue backlog. The tracker keeps active tasks and their issue references; the backlog keeps work that has not been assigned. With no `Backlog:` line, issue checks are off.

## GitHub

```md
- Backlog: GitHub issues; repo owner/repository
```

Use `owner/repository` or a GitHub repository URL. The `gh` CLI supplies authentication. `scripts/backlog.py` lists, creates, comments on, and closes issues. Writes preview by default and require `--commit`:

```sh
python3 scripts/backlog.py --config /path/to/workspace/CLAUDE.md --create "Task title" --body "Outcome, reason, and acceptance criteria" --commit
python3 scripts/backlog.py --config /path/to/workspace/CLAUDE.md --close 42 --commit
```

For a new GitHub issue, `--parent 12` and repeated `--blocked-by 7` flags create native relationships. The body is passed to `gh` on stdin. A workspace may impose its own issue template and content rules.

## GitLab

```md
- Backlog: GitLab; host https://gitlab.example.com; project group/repository; token env CHIEF_OF_STUFF_GITLAB_TOKEN
```

`host` and `project` select the API endpoint. The token value comes from the named environment variable, a macOS Keychain entry for the host, or `glab` login; the configuration contains only the variable name. The client reads, creates, comments on, and closes issues. Labels requested with `--labels` are created when needed. Writes also preview until `--commit`.

## Checks and failures

`python3 scripts/backlog.py --config /path/to/workspace/CLAUDE.md` prints open and closed counts. `--list` prints open issues; `--state closed --list` prints closed issues. A missing CLI, credential, unreachable host, malformed response, or partial result reports an unknown backlog and exits nonzero. An unknown result never counts as an empty backlog.
