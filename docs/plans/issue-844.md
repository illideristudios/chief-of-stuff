# Plan: document `chief-of-stuff backlog` for agent discovery (#844)

Stage: plan. Standard: #838. Write scope of the implementation: `scripts/backlog.py`, `evals/test_cli.py`.

## What exists

- `chief-of-stuff backlog` execs `scripts/backlog.py` (`chief_of_stuff.py` `COMMANDS`). Its `--help` today is argparse's bare listing: one description line, one-phrase option help, no output, exit-code, side-effect or example text.
- All of the behaviour below was read from `scripts/backlog.py` and `scripts/backlog_ref.py`. The baseline tests were **not run**: the shell refused `python3` (permission ask 59, unanswered), so no pass/fail counts exist yet.
- **The shared check from the command-reference issue (#838) is not in this tree.** Nothing in `evals/`, `docs/` or `scripts/` defines the required section headings or a check function, and the §11 invented-names rule is not in any repo document. This plan cannot pin the headings until that check lands.

## Behaviour the help must state (verified in code)

| Topic | Fact |
|---|---|
| Modes | No action flag: print open and closed counts (`--state`, `--labels` ignored). `--list`: one line per issue. `--create`, `--close`, `--comment`: writes. `--view N` (`--comments`): read one issue. |
| Output | Plain text, not TOON or JSON. stdout: `backlog: N open, M closed`, `#N title [labels]`, write lines (`would create: …` or `created #N: …`), `--view` text. stderr: every refusal, `backlog: unknown — <reason>`, and `nothing was written: add --commit to make these real`. |
| Exit 0 | Read answered; previews; writes that succeeded. |
| Exit 1 | A read or write failed: missing `gh`/credential, unreachable host, malformed or partial answer (5000 GitHub issues or more, over 50 GitLab pages), GitLab refusing `--parent`/`--blocked-by`. |
| Exit 2 | Usage fault: argparse errors, no config file, malformed or absent `Backlog:` line, `--close`/`--view` naming an issue outside this backlog. |
| Unknown | A failed read prints `backlog: unknown — …` on stderr and exits 1. It never prints `0 open` and is never an empty backlog. |
| Preview | Writes are previews unless `--commit`. GitHub previews make no `gh` call. A GitLab preview still reads (token check, label list). Preview lines do not echo `--parent`/`--blocked-by`. |
| `--parent`, `--blocked-by` | `--create` only. GitHub: native relationships via `gh issue create`. GitLab: refused, exit 1, even as a preview. `--blocked-by` repeats. |
| Config | `## Coordinator` block of `--config` (default `CLAUDE.md`): `- Backlog: GitHub issues; repo owner/repository` (needs `gh`), or `- Backlog: GitLab; host https://…; project group/repo; token env NAME` (token from `$NAME`, else Keychain `gitlab-<host>`, else `glab` login; never a flag). |
| Labels | GitLab `--create` makes missing labels first (when `--commit`). GitHub does not: `gh` fails on a missing label. |
| Reads and writes | Reads: the config file, the forge. Writes: the forge only, and only with `--commit` (issue, comment, close, label). No local file is written. |

## Implementation (`scripts/backlog.py`)

1. Pull the parser out of `main` into `build_parser()` so a test can enumerate its options. `main` calls it.
2. Use `argparse.RawDescriptionHelpFormatter`. Put the sections in `description` and `epilog`, under the headings #838 fixes: purpose, when not to use, options, output, exit codes, side effects, examples. Until #838 lands use these working headings and rename in one place: `Purpose`, `Not for`, `Output`, `Exit codes`, `Side effects`, `Examples`. Options stay argparse's own list.
3. Rewrite each option's `help` as `type, default: X. What it changes.`: `--config` (path, `CLAUDE.md`), `--list` (flag, off), `--state` (`opened`|`closed`|`all`, `opened`, only with `--list`), `--labels` (comma list, empty, narrows `--list`, applied by `--create`), `--timeout` (float seconds, 5.0, per request), `--create`, `--body` (text, empty, for `--create` and `--comment`), `--parent` (int, none), `--blocked-by` (int, repeatable, none), `--close`, `--comment`, `--view`, `--comments` (flag, only with `--view`), `--commit` (flag, off).
4. Not-for lines name the command to use instead. Each name is confirmed against that command's own `--help` or `COMMANDS` before it is written: tracker rows are `chief-of-stuff tracker` (read) or `log` (write); the kanban stage view is `chief-of-stuff kanban`; the full board is `chief-of-stuff board`.
5. Two or three examples, copy-paste ready, invented names only (`acme-co/ledger-demo`, "Add retry to the invoice export"): the count, `--list --state all`, and a `--create … --parent 12 --blocked-by 7` preview followed by the same with `--commit`.
6. No behaviour change beyond a `help` text and the `build_parser` extraction.

## Tests (`evals/test_cli.py`, new `BacklogHelpTest`)

- Run `chief_of_stuff.py backlog --help` as a subprocess, so the routing is covered too. Assert exit 0, empty stderr, then call the **shared check** from #838 on stdout. If the check has not landed, the test must not carry a private copy of the headings: hold the stage.
- Backlog-specific asserts on the same text: `--commit`, `--parent`, `--blocked-by`, `GitHub`, `GitLab`, `CHIEF_OF_STUFF_GITLAB_TOKEN`, and the unknown-is-not-empty sentence.
- Every option in `build_parser()` appears in the help, and each option's help states a default or says it has none.
- Every `chief-of-stuff backlog …` example line parses with `build_parser().parse_args` (shlex-split), so no example drifts out of the real flags.
- Two truth checks on the documented exit codes: no config file gives exit 2, and a `CHIEF_OF_STUFF_GH` stub that exits 1 gives `backlog: unknown` on stderr and exit 1 with stdout empty.
- Gate: `python3 -m unittest evals.test_cli evals.test_backlog`. `require_green_tests` needs passed > 0 and zero failed, skipped, xfailed. Baseline counts: unknown (not run).

## Risks and defects recorded, not fixed here

1. `--view` returns before write output prints, but `--create`/`--close`/`--comment` run first. `--view N --create T --commit` makes the issue and prints nothing about it. The help says not to combine `--view` with a write. A guard (exit 2) is a code change for a separate issue.
2. `--comments` without `--view`, and `--comment 0`, are silently ignored.
3. GitHub preview cannot predict a missing-label failure or validate `--parent`/`--blocked-by`; the help says so rather than promising a faithful preview.
4. The help text is the contract: if #838's headings differ from the working ones, the rename touches only `scripts/backlog.py`. A wrong not-for command name would mislead an agent, hence step 4's check.
5. Unverified: the §11 invented-name rule, and #838's section list, because neither is in the tree.

## Logic

```
Glossary
S := the shared check from #838 exists in the tree        H := the help carries every required section
P := the plan's headings are final                         T := the test calls S on `backlog --help`
U := a failed read prints `unknown` and exits 1            Z := the help says unknown is never an empty backlog
B := baseline tests were run                               G := require_green_tests can be met

Premises
1. ¬S                          [grep of evals/, docs/, scripts/ found no section check]
2. T → S                       [issue acceptance: the test uses the shared check]
3. P ↔ S                       [the headings are defined by S]
4. U                           [scripts/backlog.py: `backlog: unknown — …` then return 1]
5. U → Z                       [help text is generated from that behaviour; docs/backlog.md states it]
6. ¬B                          [shell refused python3; counts not taken]
7. G → B                       [counts must be known to report passed > 0, failed = 0]

Derivation
1,3  ⊢ ¬P            headings here are working names
1,2  ⊢ ¬T until S lands   implementation holds the test until then
4,5  ⊢ Z
6,7  ⊢ ¬G for this stage's report
∴ implement help text and tests as planned; rename headings and wire S when #838 lands; counts omitted from this report.
```
