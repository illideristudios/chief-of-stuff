# Plan: document `chief-of-stuff health` (#846)

Help only. `chief-of-stuff health --help` becomes the reference an agent trusts. No change to probes, advice lines, exit codes, or what is printed.

Checked against `origin/develop` at `4417a8fe`. This plan branch is `origin/main` at `50091f80`. Its `scripts/probe_health.py` still checks every runtime and has no `(advice)` lines. Apply the edit on a tree that has develop's file, and re-read `main` first. If that file has no `missing_workspace_files`, stop and report blocked.

Standard: #838 (`docs/plans/issue-838.md`). Headings: `Purpose`, `When not to use`, `Options`, `Output`, `Exit codes`, `Side effects`, `Examples`. If the checker's spellings differ, match the checker.

## What `--help` says today

On develop, `ArgumentParser` uses one description sentence and two `help=` phrases. No epilog, no `prog`, no formatter, so usage says `probe_health.py`. Nothing in `evals/` asserts that wording. The module docstring already states the contract; argparse does not show it.

## Help text

`prog="chief-of-stuff health"`, `RawDescriptionHelpFormatter`. Purpose stays the argparse description (one or two sentences): on open or resume, report whether the settings file loads, whether Claude is on the login PATH when a rotation names it, and whether each `Health:` target answers. The other prose sections go in the epilog. Options stay argparse's own list. Each flag `help=` states type, default, and what it changes. Do not rewrite probe behavior to match the help.

**Purpose.** Use it when the question is that preflight. A non-zero exit is the count of failed checks, which an agent can branch on without parsing, except exit 2 with stderr (below).

**When not to use it.** To see which worker processes are alive, use `processes`. Health does not read PIDs. To create a file an `(advice)` line names, use `init`. Health prints the command and does not run it. To audit tasks against worktrees and issues, use `audit`.

**Options.**

| Option | Type | Default | What it changes |
|---|---|---|---|
| `--config` | path | `CLAUDE.md` | File whose `## Coordinator` block is read. Its parent is the workspace root for the settings path and for advice commands. |
| `--timeout` | float, seconds | `3.0` | Per `Health:` GET only. It does not bound the settings load or the Claude lookup (`resolve` uses 15 seconds). |
| `-h`, `--help` | flag | off | Prints this text and exits 0. |

**Output.** Text lines on stdout, not TOON or JSON. Stderr is empty unless the exit is a usage fault. Order, when each line exists:

1. `settings <name> ok`, or `settings <name> absent, defaults`, or `settings <name> invalid: <why>` — only when a top-level `Settings:` bullet names a path. No such bullet, no settings line, and the file is not read. A repeated key's last bullet wins. The why is one line, the file name once, no absolute path.
2. `runtimes claude ok` or `runtimes claude missing`, or `runtimes not checked: <why>` — only when the settings file loaded and a `[models]` rotation names `claude`. Codex, Cursor, and agy are not checked. A shell fault redacts absolute paths and says neither ok nor missing. No line when Claude is not named, or when settings did not load.
3. Advice lines, zero or more (next section).
4. One line per `Health:` target, in block order, or `no Health: targets in the ## Coordinator block`.

Target lines, GET with `User-Agent: chief-of-stuff/probe`. The printed URL has no query and no credentials. `<name> <url> <status> ok` when status equals the expected status (default 200). `<name> <url> <status>, expected <n>` on a different status, including an HTTP error. `<name> <url> unreachable (<Exception>: <detail>)` when there is no status. A target that fails still gets a line.

`Health: <name> <url> [status]` is read only inside `## Coordinator`. The URL must be http or https. A line that is not that shape is a usage fault, after steps 1–3 have already printed.

**Advice lines are not failed checks.** Each starts `workspace file <name>` and contains `(advice)`. They do not change the exit code. They are printed whether or not the block has a `Settings:` line, after any settings and runtimes lines and before the targets. The name is the configured settings path, or `chief-of-stuff.toml` when the block names none. A custom path that is a regular file does not also advise the default name. Model guidance is never advised, and health does not install it.

Shapes, from `WORKSPACE_FILES` (health has no list of its own; a new entry shows up without a health edit):

- `workspace file <name> missing (advice); <command>`
- `workspace file <name> not a file (advice); move it aside, then <command>` — directory, symlink, or fifo
- `workspace file <name> unreadable (advice); <why>`

`<command>` is that entry's command with `{root}` replaced by the shell-quoted absolute workspace directory. Today's settings entry is `chief-of-stuff init --help`, which has no `{root}`. A missing `--config` file is not this line: it is a usage fault and nothing else is printed. A settings name that raises `ValueError` (a NUL) gets the settings fault and no advice line.

`absent, defaults` is also not a failed check, and it is not an advice line. It means the named file is not there, so nothing was validated and defaults apply. It never says ok.

Same path, two lines, one count: a directory is `settings <name> invalid` (one failed check) plus `not a file (advice)`. Unreadable is `invalid` (one) plus `unreadable (advice)`. Missing is `absent, defaults` (zero) plus `missing (advice)`.

**Exit codes.**

- `0`: no failed checks. Includes settings ok, settings absent, no targets, Claude ok, Claude not named, and any advice lines. Also `--help`.
- `N` (1 or more): N failed checks, stderr empty. One for settings that do not load. One for `runtimes claude missing`. One for `runtimes not checked`, however many rotations named Claude. One per target that is not ok. Advice lines add nothing. Present targets add nothing.
- `2` with stderr: usage. The failure count already computed is discarded, so exit 2 is not "two failed checks" when stderr is non-empty. Causes: `--config` is not a file (`no config at <path>`); the file is not UTF-8 (`<path>: not UTF-8 (<reason>)`, stdout empty); a `Health:` line that is not a target (stdout may already hold settings, runtimes, and advice); argparse (unknown flag, `--timeout` not a float). Exit 2 with empty stderr is two failed checks.

**Side effects.** Reads only. No dry run and no `--commit`, because nothing is written.

Reads: the config file; the settings file when `Settings:` names one, through the settings loader; Claude's binary on the login PATH when a rotation names it; one GET per `Health:` target.

Writes: none. Not the store, not the forge, not the workspace files, not the probed services. Advice does not run `init`.

**Examples.** Invented names only (§11 on develop: no real product). Copy-paste:

```
chief-of-stuff health --config ~/cedar-desk/CLAUDE.md
chief-of-stuff health --config ~/cedar-desk/CLAUDE.md --timeout 1
chief-of-stuff health
```

## Test

One method on `evals/test_cli.py`. Run `chief_of_stuff.py health --help` (`-h` exits 0, stderr empty) and call #838's shared check (`missing_sections`, and add `health` to `ENFORCED`). Do not add a second section checker. If that helper is not in the tree, stop and report blocked. #838 also writes `evals/test_cli.py`.

The same method asserts substrings the shared check will not know: `(advice)`, `absent, defaults`, `invalid`, `runtimes claude`, `no Health: targets`, `processes`, `init`, `--timeout`, and that advice does not change the exit code. It also asserts the help does not say Codex, Cursor, or agy are probed.

Run `python3 -m unittest evals.test_cli evals.test_probe_health`. `require_green_tests` needs passed > 0 and failed, skipped, and xfailed all zero. Do not edit `evals/test_probe_health.py`.

## Files

- Write `scripts/probe_health.py`: parser `prog`, formatter, description, epilog, and option `help=` only.
- Write `evals/test_cli.py`: the one test, plus the `ENFORCED` entry.
- Read `scripts/workspace.py`: `WORKSPACE_FILES`, `settings_path`. Do not edit.
- Read `scripts/claude_adapter.py`: `available`. Do not edit.
- Read `evals/test_probe_health.py`: the contract above. Do not edit.
- Read `chief_of_stuff.py`: the `health` route. Do not edit (#838 owns top-level help).

## Out of scope

Probe behavior, the advice inventory, exit codes, a new flag, a dry run, `--commit`, `docs/commands.md`, `README.md`. If #838's docs freshness test is already on, this help change makes `docs/commands.md` stale. Regenerating it is #838's `docs` command and that file is not in this issue's ownership: report that, and do not hand-edit the file.

## Risks

- **Base tree.** Help written against this plan branch would describe four runtimes and no advice lines. The issue asks for advice lines against failed checks. That behavior is `origin/develop` at `4417a8fe`. Re-read `main` before editing.
- **Shared check absent.** `missing_sections` is not on `develop` as of `4417a8fe`. The test waits for #838. A private copy of the headings would diverge.
- **`test_cli.py` conflict.** Every per-command issue adds one name to `ENFORCED`. Keep the helper; add `health`.
- **Exit 2 means two things.** Documenting 2 as usage only is false when stderr is empty. Documenting a settings fault plus a bad `Health:` line as exit 3 is false: `main` returns 2 and drops the count.
- **`absent, defaults` is not `(advice)`.** Both leave the exit code alone. Only the `(advice)` line tells the agent which command to run. Treating absent as unhealthy would be wrong. Treating `(advice)` as another failed check would be wrong.
- **Claude only.** Help that still says each named runtime is probed would match this plan branch and contradict develop.

## Logic

```
Glossary
D := the implementation tree has develop's missing_workspace_files
A := this plan branch's probe_health (every runtime, no advice lines) is what --help describes
V := an (advice) line adds 1 to the exit code
F := a settings/Claude/target failure adds 1
U := exit is 2 and stderr is non-empty
C := the test calls #838's missing_sections
P := a second section checker is added here

Premises
1. ¬D → stop                          [issue: cover advice-only lines; this branch has none]
2. □¬V                                [missing_workspace_files lines are not added to bad]
3. □(F is the only addend of N)       [main returns bad + failed; advice is printed aside]
4. U → the numeric count is discarded [TargetError and the config faults return 2]
5. □(C ∧ ¬P)                          [acceptance: the shared check]

∴ document develop's command; say (advice) and absent, defaults are not failures; say exit 2 is usage only when stderr is set; wait for #838's helper.
```
