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

## Backfill file ownership on existing GitHub issues

Before the engine takes over dispatch, use the existing backlog migration script to propose sections from each issue's text and its workspace's tracked code. Repeat `--workspace OWNER/REPO=PATH` for every GitHub workspace the engine serves; the report names its exact coverage. GitHub issue pagination is exhausted and pull requests are excluded. No workspace is inferred from the current directory.

```console
python3 scripts/migrate_backlog.py --file-ownership --workspace example/project=/path/to/project --dry-run --report docs/reports/file-ownership.md
```

The companion `file-ownership.json` is the apply plan. Commit both files to the backfill PR so the architect can review every proposed section and the issues that need the owner. A successful dry-run means the reports were produced, not that every issue was resolved. The report lists unresolved issues separately; they keep waiting for file ownership.

The script resolves explicit tracked paths, unique basenames and named Python definitions against the supplied tree. It includes existing companion tests for implementation writes. Plan citations alone cannot establish implementation scope. Explicit read-only audits receive read claims. Ambiguous names, nonexistent targets, duplicate headings and quoted ownership examples need the owner. Valid existing declarations are retained. A whole-repository scope (`.`) is never inferred.

Every proposed body is checked by `issue_gate.ownership.issue_scopes`, the same parser intake uses. The legacy parser that grants repository-wide writes on missing declarations is refused. Before #820 lands, pass `--parser-root /path/to/820-checkout`; afterward the installed script's repository is the default. The JSON plan records the three parser source hashes, code snapshot hash and original title/body hashes.

After the architect posts `Architect: APPROVED <full-current-PR-head>` on the PR, the approved plan can be applied:

```console
python3 scripts/migrate_backlog.py --file-ownership --apply docs/reports/file-ownership.json --approval-pr https://github.com/example/project/pull/123 --plan-path docs/reports/file-ownership.json --report /tmp/file-ownership-results.json
```

The command requires an architect approval on the current PR head and fetches the plan from that head to compare its exact contents. A later `Architect: CHANGES` for that head revokes approval. It rechecks approval before each write. Keep the PR head stable during apply. Issue titles and bodies are re-read; changed or closed issues are skipped. Regenerate and review a plan for changed issues and any issues opened since the snapshot. As with other GitHub issue edits, the final read and edit are separate API requests; avoid concurrent body edits during apply.

Only the ownership section is replaced; other text is preserved. Writes use `gh issue edit --body-file`, with no shell interpolation. Reapplying an unchanged plan recognizes already-applied bodies. The apply report records writes, skips and failures; a skip or failure exits nonzero and does not prevent other unchanged issues being processed. No apply run is authorized before the architect approves the backfill PR.
