# Issue #859: host adapter follow-ups

Plan stage only. Implement the three Medium findings from #857/#700 and prove every changed behavior with a failing test before changing production code.

## Baseline and prerequisites

The assigned worktree is at `50091f80`. It has no `scripts/claude_adapter.py`, `scripts/host_adapter.py`, `scripts/forge.py`, `scripts/worker.py`, `scripts/engine.py`, `evals/test_host_adapter.py`, or `evals/test_forge.py`. Do not recreate those modules on this historical base. The engine must place implementation on a base containing #857 and its subsequent fixes before implementation starts; this plan does not merge, rebase, or change the assigned checkout.

The plan is grounded in read-only Git object inspection of:

- `pr/857` at `992d12d2`: the reviewed adapter implementation and its tests.
- `origin/develop` at `4417a8fe`: the later adapter registration split (#999), worker process recovery (#901), exit timestamps, and protection checks for request targets and all protected branches (#860).
- The checkout's `CLAUDE.md` TDD rule and `evals/test_architecture.py`; the later reference's architecture sections 4, 5, and 13.

Recheck these areas on the engine-selected implementation base. Preserve the later fixes rather than copying older #857 code. Only `docs/plans/issue-859.md` is writable in this stage. Ownership below is a proposal for the implementation stage, not permission to edit those files now.

## Intended behavior

1. Every adapter uses the same host-neutral detached runner and evidence writer. Loading `host_adapter` no longer loads `claude_adapter`, even through function-local imports.
2. GitHub protection discovery includes classic protection and applicable active branch rulesets, including inherited rules. Both public protection methods use the same result. Unknown protection still refuses a non-Claude launch.
3. Startup and the end of collection sweep abandoned Antigravity `chief-<session>` MCP registrations, including registrations without a completed run record. Live sessions in any workspace retain their entries.
4. Tests for each behavior are executed red before its implementation, then green, with the commands and results recorded in the implementation report.

## Implementation sequence

### 1. Extract the shared runner

Add `scripts/process_runner.py` in Frameworks and Drivers. Move the existing detached `spawn`, cancellation, atomic evidence writer, and runner entry point out of `claude_adapter`. The module accepts prepared argv and the existing request fields; it resolves no host executable and imports neither adapter nor the runtime registry.

Move the existing `MODEL_ENV` allowlist and `model_env` helper there as well: `host_adapter` currently reaches Claude for both spawning and environments, so moving only `spawn` would leave the coupling. Preserve the credential allowlist exactly; this issue introduces no new credential source or environment policy. Both adapters import this helper. Claude's command construction, JSON parsing, classification, synchronous calls, and streaming protocol remain in the Claude adapter. Other hosts retain their own registration, argv, and classification.

Preserve the existing on-disk contract and recovery behavior:

- `command.json` is the serialized spawn request: prepared argv, cwd, session ID, and run environment. Keep its keys compatible with existing readers; do not introduce a second competing spawn record.
- The runner uses `sys.executable`, detached process-group semantics, closed stdin, and `runner.stderr` exactly as before. Its argv carries the session ID as its own token for process identity checks.
- `stdout`, `stderr`, atomic `started.json` (`pid`, `runner_pid`, `session_id`), and atomic `exit.json` retain their existing fields, including `ended_at` on the newer base.
- Runner creation failure removes only that failed attempt's launch evidence and reraises. Host creation failure still writes exit 127 evidence. Cancellation remains bounded and preserves the existing macOS handling and worker descendant cleanup.

Update `worker` to cancel through the neutral module. Retain imported compatibility names for `claude_adapter.spawn`, `cancel`, and `model_env` while existing callers/tests use them; these are aliases to the single implementation, not copies. Keep the old `claude_adapter.py --worker` entry delegating to the neutral runner for compatibility. New launches execute `process_runner.py` directly. No host-neutral path imports Claude to implement compatibility.

Register the module in the architecture test's non-rendering classification and driver circle. Add a dependency test that rejects any transitive import from `host_adapter` or `process_runner` to `claude_adapter`, and rejects adapter imports from the runner. Do not widen executable-launch exceptions: the runner executes already prepared argv and names no host binary. Architecture documents remain read-only; report the new driver placement to the architecture owner.

### 2. Read effective GitHub branch protection

First resolve an important mismatch in the issue's premise: GitHub currently documents `branches?protected=true` as including both classic protections and rulesets. The inspected code already calls that endpoint, but reads only its first 100 entries. Therefore do not claim an unverified blanket limitation in that API or write a test that merely renames a classic-protected fixture. Source: [GitHub List branches](https://docs.github.com/en/rest/branches/branches#list-branches).

Make protection discovery explicit and complete in the GitHub path of `Forge._branch_protection`:

1. Read and validate the repository's actual default branch as today.
2. Read all branches with `gh api --paginate --slurp` and `per_page=100`, retaining JSON rather than whitespace-splitting names. Validate every page and row, including the protection boolean. Require a consistent, present default branch; an incomplete snapshot raises `ForgeError`.
3. Include branches whose API `protected` flag is true. For each other branch, query the effective branch rules endpoint, with pagination and a fully URL-encoded branch path component. A nonempty, valid active-rule result adds that concrete branch to the protected set. This catches a ruleset-only default and ruleset-only non-default branches even if the listing does not mark them protected.
4. Use `GET /repos/{owner}/{repo}/rules/branches/{branch}` so GitHub resolves inherited rules, include/exclude patterns, and default-branch selectors. This endpoint returns only active applicable rules and excludes evaluate/disabled rulesets. Avoid implementing a second ruleset matcher. Source: [GitHub Get rules for a branch](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).
5. Deduplicate in deterministic discovery order. `protected_branches()` keeps its existing default-first guard contract, even when the default is unprotected; `default_branch_protected()` checks actual protection membership, never that unconditional guard entry.

A failed branch/rules read, denied access, rate limit, unsupported API, malformed JSON, or invalid schema raises `ForgeError`; it never becomes an empty successful response or permission to launch. A successful empty rules array means no ruleset protection. The GitLab path and its existing behavior remain unchanged. Reuse the same internal result shape for the worker's default and request-target checks, preserving #860's all-protected-branch head checks. Do not broaden this issue into a new definition of sufficient protection or bypass auditing.

This straightforward effective-rules approach costs up to one extra request per unprotected branch. Reuse results within a single discovery operation and short-circuit branches already reported protected. Avoid persistent caching that could keep deleted protection authoritative. If repository scale requires another strategy, obtain a revised plan rather than silently dropping pages or branches.

### 3. Sweep abandoned Antigravity registrations

Add a host-adapter sweep that enumerates the global Antigravity MCP registry and removes only engine registrations whose session is demonstrably no longer live. Use the existing CLI registration/removal path, login-shell resolution, sanitized environment, and bounded subprocess calls. Before implementing the reader, inspect the supported CLI's help or existing local implementation to establish its list/config output format; fixtures must match that format. Do not invent an undocumented JSON flag, guess a config path, dump credentials, or rewrite the whole global configuration.

Separate registry reading and liveness probing so unit tests can supply explicit registrations and process facts. Candidate names must match the engine's reserved `chief-<UUID>` format exactly. Where registration metadata is available, also validate the engine server command and session binding; preserve conflicting entries. Preserve unrelated servers, malformed names, and other user registrations.

Liveness is a machine-wide process fact, not membership in one workspace's open segments. Reuse `process_status.carriers(session_id)` and recorded identities where available. Keep entries for a live runner, host, or detached descendant, including a run from another workspace. An ended segment, missing run record, or `exit.json` alone is insufficient proof that every process is gone. If process visibility is unavailable or ambiguous, preserve the entry and report a retryable cleanup diagnostic. A successful process probe with no carriers allows removal even if the engine crashed before saving `agy_mcp` or `started.json`.

Protect the add-before-spawn interval from another engine's sweep. Serialize Antigravity registration, removal, and sweeping using a shared per-user advisory lock in the engine's state area, not a workspace-only lock. Hold it across adding the entry and starting the detached runner; release once the runner exists with the session token in its argv. Avoid nested acquisition when cleanup runs on a failed start. Use the existing lock primitive and a consistent lock order. Its default acquisition is nonblocking: contention defers a sweep or makes registration retryable, rather than allowing unbounded engine stalls. A crash releases the lock, allowing the next sweep to remove the abandoned entry. Add a lock-contention test.

Wire the sweep into:

- `engine.main`: after taking the workspace singleton lock and opening the store, before the first pass can launch workers. Run it for `--once` and normal startup, after a release handoff too.
- `worker.collect`: after processing and closing completed segments, including a pass with no completed segments. This catches crashed workers that never produced exit evidence. Preserve normal per-run cleanup and refused-start cleanup, routing removal through the same safe helper.

Missing Antigravity leaves other runtimes usable. Failed listing/removal or a timeout produces a bounded diagnostic and permits a future retry; it must not change a worker's outcome, stop collection, or falsely claim removal. Already-absent entries count as clean only when the CLI establishes that fact. Repeated sweeps are idempotent. Never sweep from a worker-controlled process or trust worker-provided paths for global configuration.

## Red-first tests and validation

For each numbered implementation slice, commit or otherwise retain the test-only diff, run the named failing tests against the unchanged implementation, and record assertion failures showing the missing behavior. A test that passes already is characterization coverage; an unrelated import or setup error is not a valid red result. Then make the minimum implementation change and rerun those same tests. Do not add all production changes first and reconstruct the TDD story afterward.

| Test file | Required cases |
| --- | --- |
| `evals/test_architecture.py` | Existing `host_adapter` transitively reaches Claude and fails the new dependency assertion before extraction; neutral runner imports neither adapter, obeys circle rules, and introduces no cycle or extra host-executable exception. |
| `evals/test_process_runner.py` (new) | Fake executable writes stdout/stderr; exact spawn/started/exit schemas and timestamp; session token survives launch; artifacts remain readable after parent loss; failed runner/host creation; atomic evidence; cancellation escalation. A pre-extraction contract test should expose the dependency/evidence gap through the existing adapter before tests are redirected to the new module. |
| `evals/test_host_adapter.py` | Every host uses the neutral runner; shared evidence stays compatible; failed Antigravity starts clean up; sweep removes a dead registration with and without run evidence; preserves a live runner/host/descendant and another workspace; unrelated/malformed names survive; unknown process status preserves; failed listing/removal/timeout retries; already-removed entry and repeated sweeps; synchronized add/spawn versus sweep. Use fake host binaries and a fake MCP registry only. |
| `evals/test_model_adapter.py` | Claude delegates detached execution to the neutral runner; existing argv, JSON/stream classification, credential filtering, cancel semantics, and compatibility entry points continue to work. Redirect tests of moved plumbing to its new owner without losing their assertions. |
| `evals/test_forge.py` | Ruleset-only default with listing flag false but effective rules present; ruleset-only non-default branch; classic-only and mixed protection; inherited/wildcard/default-selector rules resolved through endpoint fixtures; successful empty effective rules; default-first versus truly protected distinction; duplicate names; slash/Unicode names and URL encoding; more than 100 branches/rules; absent default and malformed/error/denied/rate-limited responses; GitLab regression coverage. |
| `evals/test_worker.py` | Ruleset-protected default and request target allow a non-Claude launch; empty/unknown protection still refuses; all-protected-branch tracking remains intact; collect invokes sweep even without exited workers; cleanup failures do not change reports, suite holds, or segment closure; recovered live processes retain registrations. |
| `evals/test_engine.py` | Startup sweep precedes launch in normal and `--once` modes; restart with abandoned global registration and no saved run record; another live workspace's registration survives; unavailable registry never blocks unrelated work. Mock supervisor/install state and use temporary stores. |

Run the focused suite on the implementation base:

```text
python3 -m unittest discover -s evals -p 'test_process_runner.py'
python3 -m unittest discover -s evals -p 'test_host_adapter.py'
python3 -m unittest discover -s evals -p 'test_model_adapter.py'
python3 -m unittest discover -s evals -p 'test_forge.py'
python3 -m unittest discover -s evals -p 'test_worker.py'
python3 -m unittest discover -s evals -p 'test_engine.py'
python3 -m unittest discover -s evals -p 'test_architecture.py'
python3 -m unittest discover -s evals -p 'test_orphan_hosts.py'
python3 -m unittest discover -s evals -p 'test_worker_teardown.py'
python3 -m unittest discover -s evals -p 'test_worker_health.py'
python3 -m unittest discover -s evals -p 'test_engine_mcp.py'
python3 -m unittest discover -s evals -p 'test_*.py'
```

Use the project's configured Python environment and fake integrations; no billed model call or modification to the user's actual MCP registry is required. Run the full suite once the focused suite is green, and report actual final counts, including failures/errors, skips, and expected failures. Unknown counts stay omitted; never substitute baseline counts for implementation validation.

## Risks and delivery

- **Historical base:** implementation cannot run on this checkout. The engine must supply the prerequisite code; the plan itself is complete without changing bases.
- **Recovery compatibility:** extraction can break identity probes, old runner invocations, or timestamp readers. Preserve file shapes and session tokens and run restart/orphan tests before delivery.
- **Global cleanup:** workspace-local bookkeeping can delete another workspace's live server, and registration races can delete a not-yet-started worker. Global process checks and the shared lock address both; uncertainty always preserves entries.
- **CLI output:** MCP inventory format is not established by the inspected repository. Establish it before implementation, keep parsing narrow, and make unsupported output a diagnostic rather than deletion authority.
- **Protection semantics and load:** current GitHub documentation differs from the code comment/issue premise. Capture the precise failing scenario, use active effective rules, handle pagination, and keep unreadable state unsafe. High branch counts increase API cost.
- **Parallel work:** #860, #901, and #999 already changed these surfaces in the inspected later reference. Keep those behaviors and restrict edits to the proposed paths. Architecture docs belong to the architecture owner.

Deliver in small commits by slice, with red and green evidence. Never merge. Completion requires all four acceptance items, the focused regressions and full Python suite, and a report naming any unresolved risk. This plan does not authorize product code changes during the plan stage.

## Proposed implementation file ownership

The extra write paths beyond the issue's original list are required for the neutral module, startup integration, and tests of the changed seams. Submit these exact repository-relative paths to the engine for approval.

| Mode | Path | Purpose |
| --- | --- | --- |
| write | `scripts/process_runner.py` | Single owner of detached process plumbing, environments, and evidence. |
| write | `scripts/claude_adapter.py` | Import shared plumbing and retain Claude-specific behavior/compatibility. |
| write | `scripts/host_adapter.py` | Use neutral plumbing and implement synchronized registry sweeping. |
| write | `scripts/forge.py` | Complete effective GitHub protection discovery. |
| write | `scripts/worker.py` | Neutral cancellation and collection sweep. |
| write | `scripts/engine.py` | Explicit startup sweep. |
| write | `evals/test_process_runner.py` | Shared runner contract. |
| write | `evals/test_host_adapter.py` | Adapter and global-registry regressions. |
| write | `evals/test_model_adapter.py` | Claude delegation and compatibility. |
| write | `evals/test_forge.py` | Ruleset and pagination regressions. |
| write | `evals/test_worker.py` | Launch/collect integration. |
| write | `evals/test_engine.py` | Startup/restart integration. |
| write | `evals/test_architecture.py` | Dependency enforcement and driver classification. |
| read | `CLAUDE.md` | Project TDD and ownership rules. |
| read | `docs/plans/issue-859.md` | Approved implementation plan. |
| read | `architecture/ARCHITECTURE.md` | Circles, dependency direction, and host contract. |
| read | `scripts/model_port.py` | Existing request/result contract. |
| read | `scripts/forge_port.py` | Protection-facing port and error contract. |
| read | `scripts/runtimes.py` | Preserve command/bootstrap ownership. |
| read | `scripts/shell_setup.py` | Login resolution and environment boundaries. |
| read | `scripts/process_status.py` | Global liveness and descendant checks. |
| read | `scripts/supervisor.py` | Existing state directory and advisory-lock primitive. |
| read | `scripts/guard_binding.py` | Session binding metadata. |
| read | `scripts/guard_commands.py` | Protected-branch guard contract. |
| read | `evals/test_orphan_hosts.py` | Existing detached-descendant regressions. |
| read | `evals/test_worker_teardown.py` | Teardown compatibility regressions. |
| read | `evals/test_worker_health.py` | Existing worker health regressions. |
| read | `evals/test_engine_mcp.py` | Report-channel compatibility regressions. |

## Plan-stage validation

On the unchanged historical code base, `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p test_architecture.py` passed: **12 passed, 0 failed, 0 skipped, 0 expected failures**. This is a baseline architecture check only; adapter/forge/engine implementation tests are absent from this checkout and have not been claimed as run. Check the final diff for plan-only scope and whitespace before committing.
