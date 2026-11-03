# Plan: issue #838, one help standard for the command reference

Agents learn the commands from `chief-of-stuff --help` and `chief-of-stuff <command> --help`. This plan makes those two surfaces the only place a command is described. `docs/commands.md` is rendered from them and a test fails when it is stale. Nothing here edits a command's behaviour.

## What exists

- `chief_of_stuff.py` routes 24 names through `COMMANDS` (name to script path), plus a built-in `install`. Its `--help` is one hand-written line of names, with no purposes and no groups.
- Every script builds its own `argparse.ArgumentParser`. Most use `description=__doc__` or `__doc__.splitlines()[0]`; `backlog` and `health` use a literal string. None sets `prog=`, so `chief-of-stuff kanban --help` prints `usage: kanban.py`. None has an `epilog`. `inbox` and `tracker` have subparsers.
- `README.md` repeats the command list in prose (line 12). `docs/` has no command reference.
- #646 is not in the repository, and this stage may not call the forge, so the retiring list is unknown here (see Open items).

## Design

1. **Registry.** `COMMANDS[name] = (script, group)`. `group` is one of `engine`, `intake`, `forge`, `observability`, or `None` for a command a #646 slice is retiring. `None` keeps the route working until the slice deletes the row, and the command is left out of help, docs and the check. This removes the need for a second `RETIRING` set.
2. **Purpose is not stored in the registry.** The one-line purpose is the first line of the script's module docstring, read with `ast` (no import, no execution). The script's argparse `description` already uses that line, so there is one source. A test asserts the two agree for every enforced command. `install` and `docs` take theirs from their function docstrings.
3. **Top-level help**, rendered from the registry: usage line, then for each group in the order engine, intake, forge, observability a heading and one `  name  purpose` line per command. It ends with the `<command> --help` pointer and the docs path.
4. **The required sections.** `HELP_SECTIONS` in `chief_of_stuff.py` is the one list: `Purpose` (the argparse description), `When not to use`, `Options` (argparse's own options block), `Output`, `Exit codes`, `Side effects`, `Examples`. A script supplies the four prose sections in its parser `epilog`, using `RawDescriptionHelpFormatter` and `prog="chief-of-stuff <name>"`. The scripts do not import a shared helper: they run as separate processes from `scripts/`, and the per-command issues own them. The standard is the headings, and the test is the enforcement.
5. **Generator is `chief-of-stuff docs`.** It prints `docs/commands.md` to stdout; `python3 chief_of_stuff.py docs > docs/commands.md` regenerates it. It stays in `chief_of_stuff.py`, which this issue owns, so there is no new script. Each command's help comes from a child process running `<script> --help` with `COLUMNS=100`, `NO_COLOR=1` and `PYTHONHASHSEED=0`, so wrapping and colour do not vary. The file opens with a "generated, do not edit" header and the regenerate command, then a section per group with each command's help in a fenced block.
6. **Retired commands never appear.** `docs` and top-level help skip `group is None`. When a #646 slice lands it deletes its row, and nothing else changes.

### Proposed groups (the architect confirms; one dict edit to change)

| group | commands |
|---|---|
| engine | `start`, `init`, `models`, `worktree`, `worker`, `tick`, `result`, `adopt`, `install` |
| intake | `inbox`, `backlog`, `decision`, `log` |
| forge | `kanban`, `merge-approved`, `merge-ready`, `review-threads`, `sources` |
| observability | `board`, `pages`, `processes`, `audit`, `health`, `notify`, `tracker`, `docs` |

## Steps (each is one small commit)

1. `evals/test_cli.py`: write the failing tests below first (TDD, per CLAUDE.md).
2. `chief_of_stuff.py`: registry with groups, `HELP_SECTIONS`, grouped top-level help, purpose lookup.
3. `chief_of_stuff.py`: convert `install` to the standard (its own parser lives here, so it proves the standard end to end), then add `docs`, built to the same standard.
4. `docs/commands.md`: generated once with the command above.
5. `README.md`: replace the hand-listed commands in "Install and launch" with a pointer to `chief-of-stuff --help` and `docs/commands.md`, and add two sentences on the standard and the regenerate command.

## Tests (`evals/test_cli.py`)

- **Grouped help.** Top-level `--help` lists every non-retiring command exactly once, on one line, under its group heading, in the four groups.
- **Retiring excluded.** A fixture registry with a `None` group row shows the row is routable but absent from help and from the docs output.
- **Docs fresh.** `chief-of-stuff docs` output equals `docs/commands.md`. The failure message names the regenerate command.
- **Shared standard check.** `missing_sections(help_text, name)` returns the problems: `usage:` does not start `chief-of-stuff <name>`, empty description, a missing heading, no `chief-of-stuff <name>` line under Examples. Two unit tests feed it a complete and a deficient sample, so the check can fail. `ENFORCED` is a frozenset in the test file; the test runs the check on each member's real `--help`. It starts as `install` and `docs`. Each per-command issue adds its name, which is what "turns on the check" means. Another assertion requires `ENFORCED` to be a subset of the live non-retiring commands.
- **Purpose agrees.** For each enforced command, the first docstring line is the description's first line, and is at most 100 characters.
- **Existing tests stay.** The `worker`/`start` assertions on `--help` and the pinned-release test still pass; the wrapper is untouched.

## Risks

- **Parallel merge conflicts.** Every per-command issue changes `docs/commands.md`, because its argparse text is in it. Those conflicts are resolved by regenerating, never by merging by hand. State this in the README paragraph and in the file header.
- **Python-version drift.** argparse wording and wrapping differ between Python versions (`options:` versus `optional arguments:`, colour in 3.14). The env pinning above handles colour and width. The committed file is correct only for the Python the CI runner uses; if the local and CI versions differ the freshness test fails locally. Accept it, and name the regenerate command in the failure.
- **Subparser commands.** `inbox` and `tracker` have subcommands. The standard covers each command's own `--help`; the per-command issue lists the subcommands in its `Examples`. Subcommand `--help` is not checked.
- **A script that cannot print `--help` without a workspace** fails the freshness test. The generator reports which command failed and exits nonzero rather than writing a partial file. The per-command issue fixes the script.
- **Docstring first lines are uneven** (`merge-ready` carries an issue number). They show in the top-level list until their issues are done. That is cosmetic and the 100-character cap catches the worst.

## Open items

- #646's slice list was unreadable from this stage. The implementer reads it and sets `group=None` for the named commands before generating `docs/commands.md`. If the issue does not name them, they ask rather than guess.
- The group table needs the architect's yes before the implementation merges.
- `ARCHITECTURE.md` should gain a line about the help standard. The architect owns that file, so it is not in this issue's write scope.

## Logic

```
Glossary
c   := a command that survives #646
g(c):= c has a group in {engine, intake, forge, observability}
h(c):= `chief-of-stuff c --help` carries purpose, when-not-to-use, options, output, exit codes, side effects, examples
e(c):= c is in ENFORCED in evals/test_cli.py
D   := docs/commands.md equals the output of `chief-of-stuff docs`
T   := the freshness test passes
R(c):= c is retiring (group None)

Premises
1. g(c) ↔ ¬R(c)                          [design item 1]
2. e(c) → h(c)                           [shared check, tests]
3. D ↔ T                                 [freshness test]
4. docs and top-level help read only ¬R  [design item 6; issue acceptance 4]
5. docs text is argparse's own output    [design item 5; issue acceptance 2]
6. ∀ per-command issue: it adds c to ENFORCED  [issue acceptance 3]

Derivation
from 5: commands.md holds no text that is absent from a command's --help
from 4: R(c) → c absent from help and docs
from 2, 6: after the per-command issues, ∀c: e(c) → h(c)
from 3: a stale docs file fails T

∴ every surviving command is described once, in its own --help; the docs file is a render of that, and it cannot go stale unnoticed.
```

```
Decision: put the generator in chief_of_stuff.py as `docs`, not in a new script
s := a new script scripts/gen_commands_doc.py     o := this issue's File ownership lists it
w := the generator lives in chief_of_stuff.py     f := chief_of_stuff.py is in this issue's File ownership
1. s → ¬o          [File ownership names four files; a script is not one]
2. f               [File ownership]
3. w → ¬s          [one generator]
∴ w, with no change to the ownership
```
