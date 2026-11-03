# Issue #1058: isolate flow tests from the worker workspace

## Outcome and scope

Every `flow.main` call in `evals/test_flow.py` must run independently of the caller's `CHIEF_OF_STUFF_WORKSPACE`. The empty-directory test must return zero even when that variable points to a workspace. Keep the production fallback and the worker's environment contract intact.

This planning stage changes only this file. The implementation writes only `evals/test_flow.py`; no production code, worker configuration, live workspace, tracker, or architecture changes are needed.

## Confirmed cause

Inspected revision: `17552bbdb697b84345579a3048f03efa083b64f3`.

- `scripts/worker.py:368` supplies `CHIEF_OF_STUFF_WORKSPACE` with the live root when launching a worker.
- `scripts/flow.py:332` reads that variable. If the requested root is the current directory or lacks `CLAUDE.md`, `main` switches to the configured root before checking for a Coordinator block. It can then call `tick` on the live workspace.
- `evals/test_flow.py:53` creates temporary fixtures in `FlowEngineTests.setUp` but does not isolate this environment variable. All three current `flow.main` calls are methods of this class: the normal CLI tick, the empty-directory exit, and the non-Coordinator exit. The empty-directory case triggers the fallback.
- The problem is environmental test isolation, not Settings validation or production root selection. Leaving the variable unset only in a shell command hides the regression from workers.

## Implementation and red/green sequence

1. **Add a failing regression before adding isolation.** In `evals/test_flow.py`, create a temporary workspace using `make_workspace`, with a valid Coordinator block and deliberately malformed Settings TOML, for example `[workers\n`. Use `mock.patch.dict` to point `CHIEF_OF_STUFF_WORKSPACE` at this fixture before running a fresh instance of `FlowEngineTests("test_main_exits_zero_cleanly_when_not_a_workspace")` through an explicit `unittest.TestSuite`. Capture its result and stderr and require success with no errors, failures, or stderr. Run only this named existing test inside the nested suite, so the new regression cannot recurse into itself.

   This setup reproduces worker inheritance before the inner test's `setUp`, rather than setting the variable inside a test body after isolation has already run. Before the fix, the inner assertion fails with `1 != 0` and a Settings TOML diagnostic. The bad settings stop the tick before reconciliation or dispatch, making the red case safe. Use only synthetic temporary paths, never the actual inherited workspace value.

2. **Isolate the environment for the entire test class.** At the start of `FlowEngineTests.setUp`, start `mock.patch.dict(os.environ)` and immediately register its `stop` with `addCleanup`; then remove `CHIEF_OF_STUFF_WORKSPACE` with `os.environ.pop(..., None)`. Continue with the existing temporary-directory setup. The patch preserves other environment entries while restoring the caller's original environment after the test, including when setup or assertions fail. Register cleanup before doing further work.

   Class-wide isolation covers all three existing `flow.main` calls and future methods in this class. Keep direct calls to real `flow.main`, explicit temporary `--root` arguments, and the existing normal-tick behavior assertions. Do not mock `flow.main`, change its production fallback, clear the whole environment, or depend on a CI-only unset command.

3. **Prove the regression's boundaries.** In the regression, wrap the real `flow.tick` with a spy and require that it was never called by the inner empty-directory test. Do not replace it with a stub returning success: the red case must still reach malformed settings. Assert that the poison environment value is restored after the inner suite finishes, and that the outer patch restores its prior value when it exits. The existing non-Coordinator test must still produce no stderr, and the valid CLI fixture must still dispatch through the existing mocks.

4. **Review all call sites and commit the bounded change.** Search `evals/test_flow.py` for every `flow.main` call and confirm each is covered by the fixture. Keep the regression and isolation in this one test file. Commit small, meaningful changes; never merge or remove the worktree.

## Validation and acceptance

| Check | Required evidence |
| --- | --- |
| New poison regression before isolation | Fails because the nested existing test returns `1`, with the temporary fixture's malformed Settings error. |
| Same regression after isolation | Passes; the inner test returns zero, emits no stderr, never calls `tick`, and restores the poison environment value. |
| Entire flow module with inherited poison | Run `evals.test_flow` from a Python runner that creates the same temporary malformed-settings workspace and sets the variable before loading/running the suite. Every test passes; no live workspace is read. This verifies inheritance before `setUp` for all CLI cases. |
| Environment absent | `env -u CHIEF_OF_STUFF_WORKSPACE PYTHONDONTWRITEBYTECODE=1 python3 -m unittest evals.test_flow` passes. |
| Existing real-tick coverage | `test_flow_cli_runs_tick` continues to assert the dispatch output and zero exit code using its own valid temporary workspace. |
| Full Python suite | After focused checks, run `env -u CHIEF_OF_STUFF_WORKSPACE PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_*.py'` in the supported dependency environment. This full-suite unset protects unrelated test modules; the separate poisoned flow-module run proves this issue's acceptance. |
| Scope | Diff contains only `evals/test_flow.py` for implementation. Report actual final suite counts: passed > 0; failed, skipped, and xfailed all zero. Omit unknown counts rather than claiming success. |

Do not point a validation command at the real workspace, even to demonstrate acceptance. A temporary workspace with a valid Coordinator block exercises the same fallback and offers deterministic evidence without live tracker access.

## Risks and mitigations

- **An added regression bypasses the shared fixture.** Set the poison value before invoking the existing test's lifecycle. Setting it after that test's `setUp` would undo the intended isolation and test production fallback instead.
- **Environment state leaks between tests.** Use `addCleanup` immediately after starting the patch and check restoration in the regression. Do not mutate the caller's environment permanently or clear unrelated variables, including the separate release-selection fixture.
- **The regression passes without exercising the bug.** Require the documented red Settings diagnostic before isolation; use a tick spy that wraps the real function. Retain the existing valid-workspace tick assertion.
- **Nested execution grows or recurses.** Construct a suite containing only the named empty-directory test, not the whole class or module. Capture inner diagnostics for a useful outer assertion message.
- **Scope expands into production behavior.** Preserve both the production environment fallback and worker launch environment. This issue fixes test isolation only.

## Proposed implementation file ownership

| Mode | Repository-relative path | Purpose |
| --- | --- | --- |
| write | `evals/test_flow.py` | Shared environment isolation and temporary poisoned-workspace regression. |
| read | `scripts/flow.py` | Root fallback, CLI, and tick behavior. |
| read | `scripts/worker.py` | Worker-injected workspace environment. |
| read | `scripts/settings.py` | Confirm malformed TOML is rejected before tick side effects. |
| read | `CLAUDE.md` | TDD and suite instructions. |
| read | `.github/workflows/tests.yml` | CI suite invocation. |
| read | `requirements.txt` | Supported test dependencies. |

## Planning-stage evidence

No implementation or test source changes were made in this stage.

An in-memory Python runner set `CHIEF_OF_STUFF_WORKSPACE` to a disposable workspace built with `make_workspace(..., toml_text="[workers\n")`, then ran the existing empty-directory test. Observed: one assertion failure (`1 != 0`), no unittest errors, and `flow error: chief-of-stuff.toml: Expected ']' at the end of a table declaration`. The runner asserted that this expected failure occurred. No live workspace was used.

The final baseline suite run was:

```text
env -u CHIEF_OF_STUFF_WORKSPACE PYTHONDONTWRITEBYTECODE=1 python3 -m unittest evals.test_flow
Ran 21 tests — OK
passed=21, failed=0, skipped=0, xfailed=0
```

These are focused baseline counts for the planning stage, not evidence that the implementation has already passed acceptance or the full repository suite.
