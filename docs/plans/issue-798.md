# Issue 798: bound `run_command` after SIGKILL

Read from `origin/develop` at `956a5ef6`. This worktree is `50091f80` (`origin/main`). Its `scripts/shell_setup.py` has no `run_command`. Apply the change on develop.

## Problem

`run_command` (`scripts/shell_setup.py`, the `Popen` at line 117) uses `Popen` as a context manager. After `SIGKILL` it waits at most 5 seconds, and on `TimeoutExpired` sets `child.returncode = -SIGKILL` (lines 129–133).

`Popen.__exit__` then calls `wait()` with no timeout unless the exiting exception is `KeyboardInterrupt`. `wait()` with no timeout blocks in `waitpid`. A direct child in uninterruptible sleep (D state) does not return from that. The forged `returncode` makes `_wait` return immediately, so this path does not stall, and it also skips reaping: `__del__` adds a child to `subprocess._active` only while `returncode is None`. The process keeps the trusted workspace as its cwd. `teardown` logs `str(exc)`, the same text whether the child died or is still alive.

`killpg` is caught only as `ProcessLookupError`. Any other `OSError` (a refused signal) skips the 5-second wait, leaves `returncode` unset, and `__exit__` blocks in `waitpid`.

`killpg` already signals the session. `wait` reaps only the shell. A descendant in D state does not block that `waitpid` once the shell has exited. The stall is the shell itself, which stays in D until the syscall that entered D returns. `SIGKILL` is pending until then.

## Rule

```
K := killpg has returned or raised OSError
D := the direct child is still alive after the bounded reap
W := some wait() has no timeout, including Popen.__exit__
H := that Popen is retained and later polled without waiting

1. □(K → ¬W)     [after the signal, every wait has a timeout; no context-manager wait]
2. □(D → H)      [a child that outlasts the bound is retained; returncode is not forged]
3. □(same exception propagates)
∴ a D-state child cannot hold teardown or collect inside waitpid
```

Callers get the original failure back. A stuck child is named in that failure and reaped later with `poll`, which does not wait.

## Change

`scripts/shell_setup.py`, `run_command` only.

Add `_unreaped_children: list[subprocess.Popen] = []`. On entry, drop every child whose `poll()` is not `None`. `poll` uses `WNOHANG`.

Stop using `Popen` as a context manager. Keep the stderr file's `with`. Close that file on the way out; unlinking an open file does not wait.

On `TimeoutExpired`, or any other failure while `poll()` is `None`: call `killpg`. Catch `OSError`, not only `ProcessLookupError`. Then `child.wait(timeout=1)`.

If that wait raises `TimeoutExpired`, append the `Popen` to `_unreaped_children`. Do not set `returncode`. Prefix the original exception so `str(exc)` contains `child pid N not reaped after SIGKILL`. `TimeoutExpired` formats from `cmd`, so prefix `exc.cmd`. For any other exception, append the detail to `args`. Then bare `raise`.

The success path is unchanged: read the stdout file and stderr, return `CompletedProcess`.

Leave `teardown`'s `except` list as it is. `TimeoutExpired` is already a `SubprocessError` and becomes a warning. `KeyboardInterrupt` still propagates.

Leave the docstring sentence that says teardown and stack discovery share this driver. That is #800. One clause may say that cleanup waits at most one second after `SIGKILL`.

## Tests

Add these to `evals/test_shell_setup.py`. Write each one red, then make it pass. Drive them with a `Popen` mock. A real D-state process is not portable to arrange.

1. `test_command_cleanup_is_bounded_and_preserves_the_original_failure`. `__exit__` calls `wait()`, which is what the real context manager does. `wait` records its timeout and fails if `timeout` is `None`. The first wait raises `TimeoutExpired` or `KeyboardInterrupt`. The second raises `TimeoutExpired` when stuck, otherwise sets `returncode`. Assert the same exception object, one `killpg(pid, SIGKILL)`, exactly two waits, the second timeout ≤ 1, a stuck child in the retain list with its pid in `str(exc)`, and a reaped child in neither.

2. `test_signal_failure_still_runs_bounded_cleanup`. `killpg` raises `PermissionError`. Stuck and reaped. The second wait is still `wait(timeout=1)`. The original `TimeoutExpired` propagates. Retain the child only when stuck.

3. `test_teardown_warning_distinguishes_an_unreaped_child`. The warning contains `timed out` either way, and `pid` plus `not reaped` only when the second wait times out.

4. `test_unreaped_child_is_retained_and_polled_by_later_commands`. After a stuck command the list holds that `Popen` and `poll` has not been called. The next `run_command` polls it. `None` keeps it. A later poll that returns an exit code drops it. That later command still returns its `CompletedProcess`.

These stay green with no edits:

- `evals/test_worker_teardown.py` `test_shell_timeout_also_stops_background_children`
- `evals/test_worker_teardown.py` `test_shell_rejects_unknown_shell_and_cleans_up_interrupts`
- `evals/test_worker_teardown.py` `test_shell_timeout_does_not_drain_escaped_child_pipes`
- `evals/test_worker_teardown.py` `test_finished_command_does_not_wait_for_daemon_stderr`

Run `python3 -m unittest evals.test_shell_setup evals.test_worker_teardown`, then `python3 -m unittest discover -s evals -p 'test_*.py'`. Report passed, failed, skipped, and xfailed from that full run.

## Do not build

- Another signal later. `SIGKILL` is already pending; D state defers it. `poll` is the reap.
- Forging `returncode`. That is the current shortcut, and it keeps the child out of `_active`.
- A different sentence when `killpg` itself fails. The child is still unreaped.
- An interrupt between `Popen` returning and the `try`. It predates this stall.
- #800's docstring correction.
- A real process stuck in D state.

## Risks

`test_shell_rejects_unknown_shell_and_cleans_up_interrupts` passes a `MagicMock` as `Popen`'s return value and expects `wait` to raise `KeyboardInterrupt` and then return. It does not require `__enter__`. Dropping the context manager still matches that sequence.

`test_shell_timeout_also_stops_background_children` allows 1.5 seconds. Its child dies on `SIGKILL`, so the second wait returns at once. The new tests are what lock the one-second cap. A stuck child would take about 1.1 seconds and still sit under that ceiling.

Prefixing `TimeoutExpired.cmd` changes `str(exc)` and therefore the teardown log line. Test 3 locks the new text. No other test asserts that string from `run_command`.

A child that never exits stays on `_unreaped_children` for the life of the process: one slot per stuck command, polled with `WNOHANG`, not a thread.

## File ownership

Write:

- `scripts/shell_setup.py`
- `evals/test_shell_setup.py`

Read:

- `evals/test_worker_teardown.py`
- `scripts/worker.py`
