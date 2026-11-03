# M0 spike: `claude -p --output-format json` as the model surface

Run 2026-10-09 against CLI 2.1.296, model alias `haiku` (resolves to claude-haiku-5-5). About 11 calls, about $0.03. Scripts were throwaway and are not shipped. "Verified" means observed in the run; the rest is docs or inference.

## Findings

| Item | Result |
|---|---|
| Envelope | One JSON object on stdout: `result`, `session_id`, `is_error`, `subtype`, `num_turns`, `total_cost_usd`, `usage` (tokens, cache), `modelUsage.<model>` (tokens, `costUSD`). Verified. |
| Structured verdict | `--json-schema '<schema>'` returns the answer in `structured_output`. It costs an extra turn (`num_turns: 2`) and works with `--tools ""`. Verified once; validity rate over many runs unknown, so the engine validates. |
| Session continuity | `--session-id <uuid>` on call A, `--resume <uuid>` on call B; B recalled a fact from A. Resuming an unknown id exits 1 with `No conversation found` on stderr and empty stdout. `--no-session-persistence` makes a call unresumable. Verified. |
| Bad model | Exit 1, full envelope with `is_error: true` but `subtype: "success"`. Do not key off `subtype`. Verified. |
| Budget cap | `--max-budget-usd` gives exit 1, `is_error: true`, `subtype: "error_max_budget_usd"`. It overshoots (spent $0.00035 on a $0.0001 cap). Verified. |
| Empty prompt | Exit 1, empty stdout, error on stderr. Verified. |
| Auth failure | Exit 1, `is_error: true`, "Not logged in". Verified via `--bare`. |
| Rate limit, quota, outage | Final envelope unknown. Docs describe only retry events in `stream-json` mode (`system/api_retry` with an error category such as `rate_limit`, `overloaded`, `billing_error`). **Unverified.** |
| No `--max-turns` flag | Not in this version's `--help`. |
| Isolation | By default a call loads CLAUDE.md, skills and hook context. `--safe-mode` disables CLAUDE.md, skills, plugins, hooks, MCP and agents, and auth still works. `--bare` fails here because it needs an API key. Verified. Not tested: `--setting-sources`, `--strict-mcp-config`, `--restricted`, `--permission-mode dontAsk`. |
| Cost | Per-call `total_cost_usd` and per-model `costUSD`, documented as client-side estimates. A cold default call cost about $0.012; a warm `--safe-mode` call about $0.0001 to $0.0003. Verified. |
| Stdin | With stdin not redirected the CLI waits 3 s and warns. Always pass an empty stdin. |

## Implied model port (inference)

- Request: `{prompt, model, schema?, session?: {id, resume}, budget_usd?}`. The adapter builds `claude -p <prompt> --output-format json --model M --tools "" --safe-mode`, adds `--json-schema` for a schema, `--session-id` or `--resume` for sessions, and `--no-session-persistence` when stateless.
- Response: `{ok, text, data, session_id, cost_usd, usage, duration_ms, num_turns}`, with `data` read from `structured_output`.
- Error result: `{kind: auth | bad_model | budget | usage | session_missing | unknown, message}`, derived from exit code, `is_error`, `subtype` and stderr together. Quota and outage kinds are unverified, so they map to a retryable `unknown` until one is observed.
- Session handle: the uuid. The engine can mint it with `--session-id`.

## The engine must treat defensively

- Check exit code, `is_error` and stdout parseability together; handle empty stdout with stderr text.
- Validate structured output itself.
- Budget caps overshoot.
- Pin the CLI version; flags and envelope fields change often.
- Sessions live in local disk state, so concurrent resumes and cleanup are the engine's job.
- Cost is an estimate.
- `--safe-mode` may drop auth setups other than OAuth that were not tested.

## Addendum 2026-10-10: hooks under `claude -p`, and plugin dependencies

Claude Code 2.1.296, model haiku, run in a scratch directory with a throwaway plugin loaded by `--plugin-dir`. Nothing from the spike ships.

**Command hooks run in `claude -p` workers (observed).**
- `SessionStart`, `UserPromptSubmit` and `PreToolUse` command hooks all fired in a `-p` run (a log line from each hook, in that order).
- A `PreToolUse` hook returning `permissionDecision: deny` blocked `Bash`. The worker reported the reason and did not run the command, even with `--allowedTools Bash`.
- Hook stdout reaches the model as context. The worker saw `SessionStart:startup hook success: [...]` and `UserPromptSubmit hook success: [...]` lines and could read the values out of them. A datetime-inject-style clock hook therefore works for workers, and so does session-start state injection (design decisions 9 and 11).
- **Hooks fail open (observed).** With the `PreToolUse` hook exiting 1, `Bash` ran and returned its output. The design's rule stands: the engine verifies before merge and does not rely on a hook to stop anything. Only an explicit deny decision blocks.
- Not tested: function hooks (the TypeScript mod form) and their `.catch` handler. Design decision 9 chose Python command hooks, so this only matters if a mod is ever used.

**Plugin dependencies (from the docs, not run).**
- `dependencies` in `plugin.json` is an array of `"name"`, `"name@marketplace"` or `{ "name", "marketplace", "version" }`, where `version` is a node-semver range.
- `claude plugin install A` installs and enables A's declared dependencies at the same scope. A dependency that is installed but disabled gets enabled. `claude plugin enable A` fails and prints the install command if a dependency is missing.
- A dependency from a different marketplace is not installed unless the user already has it installed and enabled at that scope, or the root marketplace's `marketplace.json` lists it in `allowCrossMarketplaceDependenciesOn`. If it is declared in `plugin.json` rather than the marketplace entry, the install completes without it and A fails to load.
- Disabling B while A is enabled is refused. Uninstalling B while A depends on it is not documented.
- Consequence for the clock: `datetime-inject` lives in the `ai-additions` marketplace and chief-of-stuff in its own. Depending on it needs `allowCrossMarketplaceDependenciesOn: ["ai-additions"]` in chief-of-stuff's `marketplace.json`, or the engine ships its own tiny clock hook in the worker guard plugin. The second needs no cross-marketplace dependency and no owner approval of a new dependency; the first is a new dependency on a plugin the owner maintains and needs the owner's yes.
- **Decision (owner, 2026-10-10: "depend on it"):** chief-of-stuff declares `datetime-inject` in `dependencies` and lists `ai-additions` in `allowCrossMarketplaceDependenciesOn` in its `marketplace.json`. No clock hook is shipped in the worker guard plugin. Declare the dependency in the marketplace entry as well as `plugin.json`, because a `plugin.json`-only declaration installs without the dependency and then fails to load. Check on install that datetime-inject arrived and enabled; the docs say nothing on uninstalling it while chief-of-stuff needs it. The version range was settled by the owner on 2026-10-16: `^1.0`.
- Sources: code.claude.com/docs/en/plugins/dependencies.md, install.md, cli-reference.md, manifest-reference.md, troubleshooting.md.

