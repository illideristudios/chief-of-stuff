# Plan: document `chief-of-stuff pages` (#847)

`chief-of-stuff pages --help` becomes the reference an agent trusts. Behavior of the server stays as it is. The only behavior change is a usage exit when `serve` is missing `--dir` or `--port`, so the exit-code section is true instead of a traceback.

#838 (still in plan) owns the shared help check and `docs/commands.md`. This issue does not generate docs, does not edit `README.md`, and does not retitle the top-level command list.

## Implementation

TDD: add the failing test in `evals/test_cli.py` first, then change `scripts/pages.py` until that test passes.

### Help text, one source

`main` builds the parser with `prog="chief-of-stuff pages"` and `formatter_class=argparse.RawDescriptionHelpFormatter`. The module docstring is the description, so the file and `--help` cannot drift. Keep the docstring's paragraphs as single lines (no hard wrap). Replace the `python3 pages.py` examples with the public command.

Argparse still prints its own option list. Each `help=` is one line that states type, default, and effect, matching the Options section. No second options table in a new module.

`--ensure` is accepted and unused: with no subcommand, `main` already calls `ensure()`. Leave that. One comment on the flag: `shortcut: --ensure is unused; the bare command already ensures; drop the flag when every caller stops passing it.` Do not make the flag required and do not make a bare `pages` stop starting the server. Agent rules pass `--ensure`.

`serve` without `--dir` or with `--port` omitted prints a usage line to stderr and returns 2, before `make_server`. Do not switch to subparsers.

### Text to put in the docstring

Purpose: Start or confirm the workspace's page server on 127.0.0.1. Use it when the Board line should be served; a GET re-renders a route when its sources change, and this command does not open the board.

When not to: Do not use `pages` to write one board or decision file, and do not use it to open a browser. Write one file without a server with `chief-of-stuff board` or `chief-of-stuff decision`. Do not run the `serve` subcommand; `--ensure` starts it detached.

Options:

| Option | Type | Default | Effect |
|---|---|---|---|
| `--ensure` | flag | off | None. With no subcommand the command already starts the server unless it is serving, and prints one status line. Pass it so the invocation matches the agent rules. |
| `--root` | path | `.` (current directory) | Workspace whose `## Coordinator` `Board:` line supplies the pages directory and port. |
| `serve` | positional, only choice `serve` | omitted | Foreground server. `--ensure` starts this detached. Agents do not run it. |
| `--dir` | path | none | Pages directory. Required with `serve`. Ignored when ensuring. |
| `--port` | int | none | Listen port. Required with `serve`. Ignored when ensuring. |
| `-h`, `--help` | flag | | This text on stdout, exit 0. Does not start a server or write a file. |

Output: Text, not TOON or JSON. Stdout is one line and never the URL: `pages: serving` (this code already answers), `pages: started`, or `pages: restarted` (an older pages server was stopped and a new one answered). `serve` prints nothing here; the detached process appends to `<root>/.chief-of-stuff/pages.log`. Stderr is `pages: not served — <reason>` on failure, and argparse's usage on a usage fault.

Exit codes: `0` status line or help. `1` the server was not started (no Board line or unreadable config; the port is not the pages server; an older server cannot be identified or stopped; the new server did not come up; the connect was denied, often a sandbox). `2` usage (unknown argument, command other than `serve`, `--port` not an integer, `serve` without `--dir` or `--port`).

Side effects: Reads the Board line, sends `HEAD http://127.0.0.1:<port>/all`, reads `<pages dir>/.pid`, and checks that pid's command line. Creates the pages directory, appends `.chief-of-stuff/pages.log`, writes `.pid` only after the server answers, and may `SIGTERM` an older `pages.py serve` on that port. Starts a detached server bound to 127.0.0.1. Routes, re-rendered on GET when a source is newer or its bytes differ from the last render: `/` today's board, else the newest; `/workers` on every GET; `/decisions` and `/decisions/<slug>`; `/issues/<n>` and `/issues/<n>/source`; `/all` lists other files. A thread refreshes `.sources.json` from the forge about every 60 seconds. `POST /decisions/<slug>` writes that decision JSON's `answer`. No store write. No `--commit` and no dry run. This command does not open a browser.

Examples (invented workspace `field-notes`, §11):

```
chief-of-stuff pages --ensure --root ~/work/field-notes
chief-of-stuff pages --root ~/work/field-notes
chief-of-stuff pages --help
```

Headings, each on its own line, so a substring check is stable: `Purpose`, `When not to`, `Options`, `Output`, `Exit codes`, `Side effects`, `Examples`.

## Test

In `evals/test_cli.py`, one class. Run `python3 chief_of_stuff.py pages --help` (this exits in argparse and must not print `pages: started`). Call the shared check from #838. If that function is already in this file, call it and do not add another. If it is absent, add `assert_command_help(text)` here: it asserts each heading above is in the text. #838 should adopt that function rather than grow a second copy.

The same test also asserts the pages-specific needles `--ensure`, `/workers`, `re-render`, and `does not open`.

One more test: `pages serve` with no `--dir` or `--port` exits 2, stderr mentions usage, and stdout is empty. That is the check for the new branch.

Do not start a server, bind a port, or add a fixture workspace. Do not run the agent eval harness.

## Risks

- #838 is not merged. Its checker may look for different heading strings. The headings above are the words in #838's acceptance (`purpose`, `when not to`, `options`, `output`, `exit codes`, `side effects`, `examples`). If #838 lands other strings first, use those and do not keep both.
- Both issues write `evals/test_cli.py`. This issue adds the `pages` test and the helper only when the helper is missing.
- Filling `help=` and the Options section says the same facts twice. That is argparse's list plus the section the shared check reads. Do not invent a formatter to suppress the list.
- A later reader may "fix" `--ensure` into a required flag. The shortcut comment is there to stop that. Callers, including the agent file, pass it, and the bare command must keep starting the server.
- `require_green_tests` is on. The implementer reports passed, failed, skipped, and xfailed from `python3 -m unittest discover -s evals -p 'test_*.py'`, with failed, skipped, and xfailed all zero.

## File ownership

- write: `scripts/pages.py`
- write: `evals/test_cli.py`
