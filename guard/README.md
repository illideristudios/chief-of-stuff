# Worker guard and engine channel

These are worker safeguards for architecture section 13, with no owner approval stage. Merges, protected pushes and stage writes stay with the engine's automated gate. The coordinator's former action list and stage yes-gates are retired.

The engine can load this directory with `--plugin-dir`. `scripts.guard_binding` exposes `Binding` / `Binding.from_env`, plus `guard_plugin_dir` and `guard_env` that `worker.launch` uses when this directory is present in the release.

The launch environment is the only record identity. Hook input and MCP arguments cannot select a different issue, stage, attempt or session. This binding is a guard rail within the worker process, not an isolation boundary against a process that changes its own environment or database files. The engine remains the authority and independently checks state before merging.

| Variable | Contract |
|---|---|
| `CHIEF_ISSUE_KEY` | Non-empty forge issue key, such as `github:example/repo#12`; the engine creates its store row before launch. |
| `CHIEF_STAGE` | Non-empty stage assigned to this worker. |
| `CHIEF_ATTEMPT` | Positive integer attempt for this stage. |
| `CHIEF_SESSION_ID` | Non-empty engine-assigned session identifier; preserved on resume. |
| `CHIEF_LAUNCHED_AT` | Required ISO 8601 UTC timestamp from the engine clock; refreshed on every launch, including resume. |
| `CHIEF_ROOT` | Absolute workspace root, holding `store.db` (the existing store CLI convention). |
| `CHIEF_PYTHON` | Engine venv Python executable, copied from `sys.executable` without resolving its symlink. Both hooks and MCP use it. |
| `CHIEF_PROTECTED_BRANCHES` | JSON array containing the forge's default branch and all protected branches. Defaults to `main`, `master`, `develop`; launch supplies any additional or different names. |

The supervisor keeps engine process state under `supervisor.state_dir(root)` and versioned venvs under `supervisor.data_dir()`. The launch helper reuses the running engine's Python, rather than deriving a venv path from the worker plugin's data directory. The engine migrates the workspace store once before launching workers; hooks and MCP never migrate it or create missing issue rows.

The hooks implement these gates:

- Bash rejects merges (including `git pull`), ambiguous or protected destination pushes, and `hub`. `gh`/`glab` pass only through an allowlist of reads and worker writes (PR/MR create, comments, comment-only reviews, checkout); within it, label, state and readiness flags are rejected. Bundled short flags and attached short-option values are denied; use separate short flags and values or long forms. Help for denied verbs is also denied. Plain HTTP clients naming a forge API are rejected. Quotes, paths to executables, assignments, `env`, `command`, `exec`, `nohup`, `time`, `timeout`, `nice`, `setsid`, supported `sudo` options, shell `-c` wrappers, pipelines and command chains are parsed without execution. Ordinary reads and explicit feature branch pushes are allowed.

- Commands outside the parser's shell subset are denied: dynamic expansion, inline interpreter programs, aliases, shell scripts, control flow, redirections, unsupported wrapper flags and malformed quoting. Forge API writes are denied conservatively because their effects cannot be established by the gate. `git config` is read-only, since command-valued keys run later inside allowed verbs. Inline interpreter and awk programs and `ext::`/`fd::` transports are denied outright, since they can build a command at runtime. A file the worker writes and then runs (a git hook, a script), or a command assembled from encoded text by a shell expansion, is outside what a command gate can see. These restrictions are guard rails; they do not replace engine verification, which is the boundary.

- `AskUserQuestion` is denied with instructions to call MCP `ask`. `PermissionRequest` also appends an unanswered ask containing the requested tool and input, then denies. Neither can approve an action.

- Stop blocks until a report matches all four identity fields and was created at or after `CHIEF_LAUNCHED_AT`. A repeated Stop with `stop_hook_active=true` is allowed to prevent a hook loop. Store or binding failures produce an explicit deny/block/context message. Host timeouts and process crashes can still skip command hooks, as recorded in design decision 9.

- SessionStart injects issue identity, title, assigned stage and attempt, cached forge stage, all asks with their answers, and previous reports. Answers and findings are store data for resume context.

The MCP server runs over stdio and exports only `report` and `ask`:

- `report(outcome, summary, findings=[], passed=None, failed=None, skipped=None, xfailed=None)` accepts `done`, `failed` or `blocked`. The four counts are the final suite run, non-negative integers, omitted when unknown. Findings require severity from `policy.SEVERITIES`, title, evidence and suggestion; path and a positive line number are optional. Findings reject unknown fields. Reports append with the bound identity; late older attempts cannot overwrite newer reports. Queries order attempts oldest to newest and can filter the exact run.

- `ask(question, options=[], context="")` immediately returns an ask id and tells the worker to report blocked, stop and wait. The answer starts as null. Decision delivery and answer resolution belong to M5's trio gate. The channel never grants permission.
