# Issue #839: document `init` for agent discovery

Stage: plan. This stage changes only this file; implementation, runtime behavior,
and generated references remain untouched.

## Outcome and scope

Make `chief-of-stuff init --help` a complete, trustworthy reference under the
shared help contract from #838. Keep the reference in the initializer's argparse
definition, exercise it through the public command, and use #838's shared check.
Document behavior without changing initialization semantics, introducing flags,
or implementing the parent engine from #825.

## Evidence and dependencies

- `chief_of_stuff.py` routes `init` to `scripts/init_workspace.py`; its current
  help shows `init_workspace.py` as the program name and omits most defaults and
  all output, exit-code, side-effect, and example sections.
- `scripts/init_workspace.py` owns option validation, Coordinator generation,
  settings creation, model guidance installation, and optional hold-label
  provisioning. `scripts/install_model_guidance.py`, `scripts/settings.py`,
  `scripts/backlog.py`, and `scripts/backlog_ref.py` define supporting behavior.
- Existing behavioral coverage is in `evals/test_init_workspace.py`; public
  routing and installed-release coverage are in `evals/test_cli.py`.
- #838 is open at planning time. It owns the shared section check in
  `evals/test_cli.py` and generation/staleness checking of `docs/commands.md`.
  Integrate with its actual helper, section names, formatter, command registry,
  and generator once available; do not invent a second checker or handwrite the
  generated reference. Coordinate edits to the shared test file with #838.
- #825 is also open. Its acceptance specifies a parent `[workspaces]` name-to-path
  table, relative to the parent root, with fully configured child workspaces and
  one parent engine. The checked-out architecture document ends at section 9;
  sections 11 and 12 are absent, including from the default-branch copy checked
  during planning. Re-read the landed architecture and #825 implementation before
  documenting operational parent setup. Use invented examples as #839 explicitly
  requires. Do not claim that this checkout already supports parent engines.

Sources: [#839](https://github.com/illideristudios/chief-of-stuff/issues/839),
[#838](https://github.com/illideristudios/chief-of-stuff/issues/838),
[#825](https://github.com/illideristudios/chief-of-stuff/issues/825).

## Implementation sequence

1. After #838 lands, enable its shared contract check for `init` in
   `evals/test_cli.py`. First demonstrate that the current help fails it. Add
   focused assertions for the init-specific facts below rather than a brittle
   snapshot of whitespace or the entire help message.
2. Expand the argparse help in `scripts/init_workspace.py`, using #838's exact
   conventions. Set the public program name to `chief-of-stuff init` and preserve
   readable section breaks and copy-paste-ready examples. Describe purpose and
   when to use it in one or two sentences: bootstrap configuration inside an
   existing workspace directory that has no Coordinator block. In the explicit
   when-not-to section, direct an already configured workspace needing only model
   guidance to `chief-of-stuff models --root PATH`; initialization is not an
   update or reset operation. Reconfirm that alternative still exists after
   command retirements from #646.
3. Give every option explicit type, requiredness/default, and effect, including
   constraints and interactions in the inventory below. Keep the existing parser
   behavior and defaults unchanged. Explain that launcher and worker-mode flags
   affect newly created settings only.
4. Add output, exit codes, side effects, and three examples as specified below.
   Keep these descriptions beside the command implementation as the authoritative
   source. Do not duplicate the prose manually in README or another guide.
5. Once #825 lands, include parent setup in the same help: initialize each child
   separately, add an invented mapping such as `alder = "projects/alder"` under
   `[workspaces]` in the parent's settings, and run
   `chief-of-stuff engine --root PARENT`. Explain that each child retains its own
   CLAUDE.md, backlog, store, policy, and settings; mappings are parent-relative,
   and `init` does not register children or start services. Verify these statements
   against the landed code, including parent initialization and its optional own
   backlog. Add no parent flags or engine functionality in this issue. If #825 is
   still pending, identify the dependency explicitly and leave this acceptance
   item incomplete rather than documenting unavailable behavior as operational.
6. Run #838's generator to update `docs/commands.md`, and run its freshness check.
   Include only generated differences attributable to init. Commit the help and
   tests in small coherent changes; never merge.

## Option inventory to document

This inventory describes the planning checkout; reconcile any option changes
from landed dependencies before implementation.

| Option | Type / default | Effect and constraints |
| --- | --- | --- |
| `-h`, `--help` | Boolean action; false | Print help and exit 0; other required arguments need not be supplied. |
| `--root` | Path; required, no default | Resolve an existing workspace directory; init does not create the root. |
| `--user` | String; required, no default | Set Coordinator user; trimmed, nonempty printable line without backticks or pipes. |
| `--timezone` | IANA timezone string; required, no default | Set Coordinator timezone; must be recognized by ZoneInfo. |
| `--daily-dir` | Relative directory string; `daily/` | Set daily log and tracker paths; normalized trailing slash, stays inside the workspace. |
| `--worktrees` | Relative directory string; `worktrees/` | Set Coordinator worktree location; same path restrictions; does not create worktrees. |
| `--agent` | Repeatable `NAME:task\|standing` string; empty list | Append agent declarations; names match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. |
| `--launcher` | Choice `ghostty` or `tmux`; `ghostty` | Set `[workers].launcher` in new settings; starts no terminal. |
| `--worker-mode` | Choice `interactive` or `one-shot`; `interactive` | Set `[workers].mode` in new settings; dispatches no worker. |
| `--board-port` | Integer; unset | Add local Pages board configuration at `http://127.0.0.1:PORT/`, directory `pages/`; valid range 1–65535; neither serves nor opens the board. |
| `--github-repo` | `owner/repository` string; unset | Select GitHub backlog; mutually exclusive with `--gitlab-host`; GitHub calls use `gh` authentication. |
| `--gitlab-host` | HTTP(S) URL string; unset | Select GitLab backlog; requires `--gitlab-project`. Current parser accepts HTTP as well as HTTPS. |
| `--gitlab-project` | Group/project string; unset | GitLab project path; requires `--gitlab-host`. |
| `--gitlab-token-env` | Environment-variable name string; `CHIEF_OF_STUFF_GITLAB_TOKEN` | Record the credential variable name in the generated GitLab Backlog line, never a token value; validate as an environment-variable identifier. |
| `--dry-run` | Boolean action; false | Validate and print intended file changes and optional label intent, with no file writes or forge calls. |

No forge flags means no Backlog line. There is no `--commit` flag: normal init
performs its writes immediately. Do not imply that existing settings are replaced
when options differ from their contents.

## Output and exit contract

- Format is human-readable text, not TOON or JSON; there are no structured output
  fields. Enumerate the meaning of each text component instead.
- Successful writes print optional hold-label creation notices, then
  `configured ROOT; settings in SETTINGS; model guidance in GUIDANCE` on stdout.
  Explain the resolved workspace path and both destination paths.
- Dry-run stdout describes create versus append for CLAUDE.md and prints the
  proposed Coordinator block; describes create versus keep for settings and prints
  new settings only when absent; describes create versus keep for model guidance;
  and optionally prints hold-label intent. Guidance contents are not previewed.
- Help goes to stdout. Validation/filesystem refusals normally use
  `refused: ...` on stderr; argparse usage faults print usage and an error to
  stderr. Forge hold-label failures warn on stderr while initialization continues.
- Exit 0: successful initialization, successful dry-run, or help; includes a
  nonfatal hold-label provisioning warning. Exit 1: refused configuration or I/O
  failure. Exit 2: argparse usage fault, including missing required arguments,
  unknown options, invalid choices, non-integer ports, and mutually exclusive
  forge selections. Distinguish semantic validation (for example, a missing
  GitLab partner flag or an integer port outside its valid range) as exit 1.

## Side effects to make explicit

- Reads the root and existing CLAUDE.md, existing settings for validation and the
  configured hold label, and the shipped model-guidance template when creating
  guidance. Reads timezone data for validation.
- Appends one Coordinator block to existing CLAUDE.md, preserving preceding
  content while normalizing trailing whitespace; creates it with a Workspace
  heading if absent. Refuses to replace an existing Coordinator block.
- Creates `chief-of-stuff.toml` only when absent (notifications off, selected
  worker defaults and commented examples); preserves valid existing settings.
  Creates `chief-of-stuff-models.md` only when absent; preserves existing guidance.
- Does not create a store, issues, repositories, daily files, worktrees, board
  files, or running services. It does not install/start an engine or supervisor,
  launch workers, or open browser pages. Recheck store behavior after dependencies
  land rather than copying this assertion blindly.
- A selected forge plus a nonempty existing `[kanban].human_review_label` can
  cause label reads and creation of a missing hold label. New default settings do
  not configure this label. This is an immediate remote write without `--commit`;
  preview reports intent without contacting the forge. Authentication follows the
  actual forge adapter; no credentials are written into configuration.
- Do not promise atomicity or rollback: label provisioning precedes local writes,
  and sequential local writes can leave partial results on I/O failure.

## Examples

Include exactly three self-contained public-command examples, each using an
existing directory created for the example and invented user/project names:

1. Preview an Alder workspace for Robin in `America/Chicago` using `--dry-run`.
2. Initialize a separate Birch workspace for Morgan with
   `--github-repo example-team/birch`, `--agent implementer:task`, and
   `--launcher tmux`.
3. Initialize a separate Cedar workspace for Casey with
   `--gitlab-host https://gitlab.example.test --gitlab-project example-team/cedar`
   and `--gitlab-token-env CEDAR_GITLAB_TOKEN`.

Use distinct temporary roots so running one example cannot make the next fail
with an existing Coordinator block. Keep the parent mapping as a setup snippet
within the side-effects/setup discussion to preserve the two-or-three-example
limit. No real customer names, credentials, deployment names, or external writes
are needed to test these examples.

## Tests and completion checks

- In `evals/test_cli.py`, invoke the public entry point with `init --help`, no
  workspace arguments, and an empty temporary working directory. Assert exit 0,
  empty stderr, no created files, and all sections through #838's shared check.
- Add focused help assertions for option completeness, required/default metadata,
  all forge interactions, preservation/refusal behavior, immediate writes versus
  preview, stdout/stderr and text output, the three exit codes including nonfatal
  label failures, and invented examples. Use semantic assertions robust to wrapping.
- Validate representative exit/output statements using temporary workspace
  subprocesses: missing required arguments or conflicting forge flags return 2;
  invalid paired GitLab arguments return 1; dry-run returns 0 and leaves no files.
  Reuse existing behavioral coverage where sufficient. Mock forge access for any
  label case; do not call real forges from tests.
- After #825 lands, assert the parent setup guidance through the shared help
  surface, including relative child paths, independent child configuration, and
  the parent engine command. Parent execution belongs to #825's tests.
- Run `python3 -B -m unittest evals.test_cli evals.test_init_workspace`, then
  #838's generation/freshness checks, then the repository suite:
  `python3 -B -m unittest discover -s evals -p 'test_*.py'`.
- Review rendered terminal help and generated reference for readable headings,
  all option metadata, and usable examples. Confirm the diff contains only
  implementation-owned files. Report actual final suite counts, including skips
  and expected failures; do not infer a green suite from help assertions alone.

Planning validation: the existing combined CLI and initializer suite passed:
22 passed, 0 failed, 0 skipped, 0 xfailed. No implementation assertions were added
or run during this plan stage; the full repository suite was not run.

## Risks and ownership proposal

- Dependency timing: #838 determines the actual shared API and generator, and
  #825 determines supported parent behavior. Reconcile both before declaring all
  implementation acceptance items complete. The missing architecture sections
  must be reread when available.
- Documentation drift: settings, guidance, command retirement, and parent support
  can change before implementation. Re-inventory the live parser and actual
  writes, then regenerate the reference from that same checkout.
- Existing edge cases must not turn a documentation issue into behavior repair:
  the current guidance destination can raise an uncaught ValueError, and the
  hold-label GitLab object uses its default token-variable name even when init
  records a custom `--gitlab-token-env` in CLAUDE.md. Avoid promising universal
  refusal formatting or custom-variable use by that provisioning path. If these
  remain, identify them separately rather than silently changing semantics here.
- Scope/conflicts: #838 also owns `evals/test_cli.py`; coordinate its landed
  helper before editing. `docs/commands.md` is proposed as an additional generated
  write because #838 requires freshness after command help changes.

Proposed implementation writes:

- `scripts/init_workspace.py`
- `evals/test_cli.py`
- `docs/commands.md` (generated only, once #838 supplies it)

Proposed implementation reads:

- `chief_of_stuff.py`
- `scripts/install_model_guidance.py`
- `assets/worker-model-guidance.md`
- `scripts/settings.py`
- `scripts/workspace.py`
- `scripts/backlog.py`
- `scripts/backlog_ref.py`
- `evals/test_init_workspace.py`
- `architecture/ARCHITECTURE.md`
- `README.md`
- `.github/workflows/tests.yml`

Use #838's actual generator path when it lands and add it as read ownership if
it is a separate file; no speculative generator filename is claimed here. Add
read ownership for #825's engine/setup modules when their paths are available.
