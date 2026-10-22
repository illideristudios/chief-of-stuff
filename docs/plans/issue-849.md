# Issue #849: document `limits` for agent discovery

## Goal and scope

Make `chief-of-stuff limits --help` the trusted reference for collecting account-limit readings. Change help text and its regression coverage, preserving collection, formatting, persistence, and exit behavior. Do not add probes, flags, output formats, warning logic, or a second handwritten command reference.

This planning stage changes only this file. Implementation ownership is proposed below.

## Evidence and prerequisites

- The assigned checkout is at `50091f80`. It has no `scripts/limits_cli.py`, no `limits` entry in `chief_of_stuff.COMMANDS`, and no shared command-help check in `evals/test_cli.py`. Implementation must start from a base containing the runtime-limits command; recreating that feature is outside #849.
- [#838](https://github.com/illideristudios/chief-of-stuff/issues/838) is open. It owns the shared required-section check, command grouping, help-derived reference generator, and stale-reference test. Adopt its actual helper and section conventions after it lands; do not implement a competing checker here. Its helper name and generator invocation are not yet established.
- To ground this plan, inspected upstream `develop` at `4417a8fedba45a1460e172913c1e73a26bfabe00`: `limits_cli.py`, `limits_probe.py`, `limits_history.py`, `limits.py`, `session_logs.py`, `settings.py`, `workspace.py`, the CLI router, existing limits tests, and architecture sections 10 and 11. Recheck these contracts on the eventual implementation base.
- Architecture section 10 defines account-wide readings and history; section 11 requires invented workspace names. Other runtimes' future probes belong to #757, not this issue.

## Implementation

1. In `evals/test_cli.py`, enable #838's shared check for `limits` and add focused semantic assertions described below. Run the new tests first and confirm that today's terse help fails them.
2. Update only the parser documentation in `scripts/limits_cli.py`: use the shared section headings, a readable multiline epilog/formatter, and explicit argument help. Display the installed invocation `chief-of-stuff limits` in usage and examples. Keep parsing ahead of settings access and collection.
3. Render the help and review it against the following content contract. Wrap prose for terminal readability while preserving copy-paste command lines.
4. Use #838's generator to refresh the checked-in command reference, if present, and run its freshness check. `docs/commands.md` is generated output only; do not write its prose separately. Shared tooling, other commands, and README changes remain with #838.

### Help content contract

| Section | Required content |
| --- | --- |
| Purpose / when to use | One or two sentences: take one unbilled, local reading for each runtime under its configured account, record it, and print it; use this to inspect available quota before dispatch. These are locally observed readings, not a live provider query. |
| When not to use | Name a real alternative: use `chief-of-stuff health` for settings/runtime/Health-target diagnostics. State that `limits` is not a connectivity or configuration health check. Revalidate that command's availability on the implementation base. |
| Options | `--root PATH`: filesystem path, default `.` (current working directory), selects the workspace whose `CLAUDE.md` and named Settings TOML determine the launch account. Missing configuration uses defaults. `-h, --help`: boolean action, absent by default; prints help and exits without collecting or recording. These are the complete current options; do not invent account, format, history, dry-run, or commit flags. |
| Output | Plain text on stdout, not TOON or JSON. Explain the six header fields listed below and that one runtime can yield multiple limit rows. Unknown readings carry their reason inline on stdout, not as command failures. Normal completion leaves stderr empty; command failures print `limits: <reason>` on stderr; argparse usage faults print usage/error on stderr. Help is text on stdout. |
| Exit codes | `0`: readings recorded and printed, even if some/all readings are unknown; also successful help. `1`: command failure such as unreadable/invalid settings or shared-history creation/migration/write failure, diagnosed on stderr. `2`: argparse usage fault, including unknown options or a missing `--root` value. Quota usage itself is not a nonzero status. Preserve these behaviors. |
| Side effects | Read workspace `CLAUDE.md`, its Settings TOML when named, and the selected account's local session logs through `session_logs.py`. Normal invocation creates/migrates/updates the account-wide SQLite history and its parent directory/migration lock as needed. No issue-store or forge changes, service requests, runtime launches, or billed model calls occur in collection. There is no preview/dry run and no `--commit` gate: a normal invocation records immediately. Help performs no collection/history writes. Distinguish any installed-launcher venv bootstrap from collection itself. |
| Examples | Provide the three commands below with short descriptions; use invented workspace names. |

Output fields must be described as actually displayed, not as an invented serialized `Reading` object:

- `runtime`: host identifier, currently `claude`, `codex`, `cursor`, or `agy`.
- `account`: selected account/profile name; `default` is displayed for the internally empty default-login identifier.
- `limit`: the source's window/model/cap identifier, or the generic `limits` identifier when unavailable.
- `used/cap`: numeric usage and cap followed by the unit, e.g. `85/100%`; unavailable usage is `unknown: <reason>`, never zero.
- `reset`: reset timestamp in ISO 8601 form, or `unknown` when absent.
- `observed`: ISO 8601 observation timestamp (`read_at`); a known local event retains its event time, so rerunning the command does not make old evidence fresh.

Account, warning, and history details must be discoverable in these sections:

- A pass reads each runtime once under the account the workspace launches with. Claude uses `[workers] claude_as_profile`; other runtimes currently use their default login. This is not an enumeration of every account installed on the machine. Multiple workspaces sharing an account share history; distinct runtime/account series remain separate.
- Claude reads structured local transcript events. The default source is `$CLAUDE_CONFIG_DIR` or `~/.claude`; a selected profile uses `$CLAUDE_AS_PROFILES_DIR/<profile>` or `~/.claude-profiles/<profile>`. Do not imply comprehensive provider reporting. Unreadable, missing, malformed, unsupported, or expired observations can yield unknown with a reason. Currently codex, cursor, and agy report `unknown: read in 1.1`; verify this wording against the implementation base.
- `[limits] warn_at` is a finite numeric fraction in the inclusive range `0..1`, default `0.8`. Downstream near-cap judgments compare observed used/cap or projected usage at reset with this threshold. The CLI validates settings and records/prints readings; it does not itself print a threshold verdict, raise an interrupt, enforce a cap, or change its exit status when usage is high. Unknown readings cannot establish a near-cap warning.
- The shared history is `$XDG_STATE_HOME/chief-of-stuff/limits.sqlite`, default `~/.local/state/chief-of-stuff/limits.sqlite`. `--root` chooses account/configuration, not a workspace-local history location. State that this database is separate from the issue store. Repeated source observations and unchanged unknown reasons may be deduplicated; recording does not promise an additional row each time.

Proposed copy-paste examples:

```text
chief-of-stuff limits --help
chief-of-stuff limits
chief-of-stuff limits --root /tmp/cedar-workspace
```

Describe these as discovering the contract, recording the current workspace's readings, and recording the account selected by an existing invented workspace, respectively. A normal example records immediately; do not describe it as a preview or use example configuration that silently selects a different account.

## Tests and validation

- Reuse #838's shared check to assert all required sections on the public `limits --help` surface; no local duplicate list/checker. Verify exit `0` and empty stderr. Use the shared public-CLI harness so installed venv bootstrapping is isolated from real user state and network access.
- Add semantic assertions for `--root` type/default/effect and help action; all six displayed fields; text/stdout/stderr; exits `0/1/2`; immediate persistence/no dry run/no commit gate; account selection; unknown with reason; `warn_at` default/range; and both XDG/default history paths. Normalize whitespace where wrapping varies; avoid a full help snapshot.
- Call `limits_cli.main(["--help"])` with settings loading, `read_all`, and `LimitsHistory` mocked. Require `SystemExit(0)` and no calls to those collaborators. Run a parallel parser-fault case (`--root` without a value or an unknown flag): exit `2`, usage on stderr, no collection/history access.
- Keep behavior cases in `evals/test_cli.py` for mixed known/unknown readings (six-field text, inline reason, success), and a mocked history failure (exit `1`, diagnostic on stderr, no success table). Isolate history and account sources with mocks/temporary directories; never inspect the developer's real transcripts or mutate shared history. Preserve the existing `evals/test_limits.py` coverage of profile selection, persistence, deduplication, and settings validation.
- On the implementation base run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest evals.test_cli evals.test_limits -v`, #838's generated-reference check, and the repository Python suite: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_*.py'`. Use the repository's configured dependency environment if required. Record final counts, including skipped and expected failures.
- Manually review public help for section clarity, literal copy-paste examples, actual path/default wording, and agreement with captured known/unknown output. Run the generator twice if needed to establish that generated output is stable; do not hand-edit it.

Planning-stage baseline on the assigned checkout: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest evals.test_cli -v` completed with **9 passed, 0 failed, 0 skipped, 0 xfailed**. This validates the existing CLI baseline only; no limits implementation or #838 help check exists here to test yet.

## Risks and sequencing

- **Base mismatch:** the assigned checkout predates the entire feature. Carry this plan forward onto the engine-selected implementation base containing runtime limits; do not change this plan-stage checkout's base or expand this stage into implementation.
- **Shared dependency:** #838 must establish its helper/formatter conventions and generator before this issue can finish. Use its real interfaces and include generated-reference freshness in acceptance. Shared edits in `evals/test_cli.py` need reconciliation with other command-documentation issues.
- **Misleading safety claims:** the probe is unbilled but the command writes history immediately. Help must make this clear and avoid calling the whole invocation read-only. The public router can also bootstrap its dependency venv; inspect the shared launcher contract before promising help has no filesystem effects at all.
- **Stale observations and runtime support:** local timestamps and explicit unknown reasons must not become claims of current live quota or zero usage. Recheck #757 changes before documenting the runtime-specific unknown reason.
- **Threshold confusion:** `warn_at` belongs to downstream judgments; documenting it must not imply this CLI emits warnings or implements budgets. No behavior change is needed.
- **Scope and rollback:** proposed changes are parser prose, tests, and generated reference only. Reverting them restores prior discovery behavior without data migration. Do not modify runtime probes, history schema, engine dispatch, or settings validation.

## Proposed implementation file ownership

| Mode | Repository-relative path | Reason |
| --- | --- | --- |
| write | `scripts/limits_cli.py` | Canonical help and argument descriptions. |
| write | `evals/test_cli.py` | Enable shared help check and add semantic/side-effect regressions. |
| write | `docs/commands.md` | Generated-only refresh if #838 checks in the reference; needed for its stale-reference gate. |
| read | `chief_of_stuff.py` | Public routing, store-backed dependency setup, installed command behavior. |
| read | `scripts/limits_probe.py` | Runtime/account selection and unknown semantics. |
| read | `scripts/limits_history.py` | Shared path, persistence and deduplication behavior. |
| read | `scripts/limits.py` | Reading fields and pure threshold judgments. |
| read | `scripts/session_logs.py` | Local source locations and account isolation. |
| read | `scripts/settings.py` | Account settings, `warn_at` default and validation. |
| read | `scripts/workspace.py` | Settings-path resolution. |
| read | `scripts/probe_health.py` | Correct alternative command and invocation. |
| read | `evals/test_limits.py` | Existing behavior coverage to preserve and run. |
| read | `architecture/ARCHITECTURE.md` | Account-wide limits contract and invented-name rule. |

The reference generator's path is not defined by #838 yet. Once established, use its documented entry point and add read ownership if the engine requires it; no generator changes are proposed here.
