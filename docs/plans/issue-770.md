# Issue #770 Plan: Run Eval Fixtures in an Isolated Environment Where Only the Git Shim Is Reachable

```
I: .github/ci/Dockerfile relocates system git to /usr/lib/git-core/git-real and installs .github/ci/git at /usr/bin/git (/bin/git) and /usr/lib/git-core/git*
T: .github/ci/git routes eval-fixture git calls (CHIEF_OF_STUFF_PATH_PREFIX in env or ancestor shell /proc/<pid>/environ) through {prefix}/git before any execution
S: evals/run.py stays stack-independent (§11); GIT_SHIM records every argv to calls.jsonl and refuses non-allowlisted calls; _path_git_write grades rec.mock_calls
C: (I ∧ T ∧ S) ⇒ absolute-path and PATH-reset git writes in CI (/usr/bin/git push, /bin/git push, /usr/lib/git-core/git-push) are recorded and refused; direct local runs remain PATH-only
```

## 1. Context and Architect Ruling

Issue [#770](https://github.com/illideristudios/chief-of-stuff/issues/770) was split from [#702](https://github.com/illideristudios/chief-of-stuff/issues/702) (landed on `develop` in PR [#730](https://github.com/illideristudios/chief-of-stuff/pull/730), commit `320d4a06`) to close the absolute-path bypass (`/usr/bin/git push`, `/bin/git push`, `PATH=/usr/bin:/bin git push`, `command -p git push`) around the eval harness's `PATH` `git` shim.

The canonical Architect ruling on [#770 (`#issuecomment-6236983418`)](https://github.com/illideristudios/chief-of-stuff/issues/770#issuecomment-6236983418) fixes the boundaries for this work:

- **Placement**: A repository-only CI driver under `.github/ci`. `evals/run.py` stays stack-independent (`ARCHITECTURE.md` §11) and learns nothing about OS or container isolation (`docker`, `bwrap`, `sandbox-exec`).
- **The shape**: A CI job builds an environment whose only reachable `git` at standard paths is the shim, with no system `git` directly reachable at `/usr/bin/git`, `/bin/git`, or `/usr/lib/git-core/git*`, and runs `python3 evals/run.py ...` (and the eval unit test suite) unchanged inside it. The grader (`_path_git_write`) keeps reading the shim's call record (`rec.mock_calls` from `calls.jsonl`; no shell-text parsing per #710).
- **Local vs CI**: Locally, `evals/run.py` keeps its documented `PATH`-only limit. A developer who wants the full guarantee runs the `.github/ci` driver.
- **Acceptance** (judged in CI):
  1. An absolute-path git write in a fixture (`/usr/bin/git push`, `/bin/git push`) is **recorded** in `calls.jsonl` and **refused** before any git write runs.
  2. The grader's limit note is updated to state that the guarantee holds in CI and that direct local runs are `PATH`-only.

### Branch Baseline Note

The `GIT_SHIM`, `_git_read_only`, and `_path_git_write` changes from #702 / #726 / #730 live on `origin/develop` (`956a5ef6`). If the implementation worktree starts from `origin/main` (`50091f80`) rather than `origin/develop`, the implementer must ensure the worktree includes `origin/develop` (or fast-forward/cherry-pick the #730 base) before applying the steps below so `GIT_SHIM`, `_path_git_write`, and `.github/ci/Dockerfile` match `origin/develop`.

---

## 2. Design Overview

### Why `/usr/bin/git` Must Route Through `{CHIEF_OF_STUFF_PATH_PREFIX}/git` Rather Than Failing with `ENOENT`

1. **Recording and grading contract**: `_path_git_write` (`evals/run.py:369-380`) grades `rec.mock_calls`, which is populated from `calls.jsonl` written by `GIT_SHIM` (`evals/run.py:1393-1411`). If `/usr/bin/git` were simply deleted from the container (`ENOENT`), a fixture running `/usr/bin/git push` would fail at the shell level without writing to `calls.jsonl`, leaving `rec.mock_calls` empty and causing `run.grade({"type": "path_git_write"}, rec)` to falsely return `True`.
2. **Allowlisted reads and harness setup**:
   - Before `eval_environment` is entered, `evals/make_repo.py` and `evals/test_cases.py` run `git init`, `git add`, `git commit`, `git push`, and `git remote` using standard `PATH`.
   - Inside `eval_environment`, `GIT_SHIM` delegates allowlisted read-only calls (`status`, `log`, `diff`, `show`, `rev-parse`, `branch --list`, `remote -v`, `config --get`, `ls-files`, `cat-file`) to the next `git` on `PATH` (`evals/run.py:1405-1409`) with `GIT_OPTIONAL_LOCKS=0`.
   - Trusted engine helpers (`scripts/git_trees.py`) call `git` with `audit_env()` / `write_env()`, which strip `CHIEF_OF_STUFF_PATH_PREFIX` and set `GIT_NO_REPLACE_OBJECTS=1`.

### Two-Layer Architecture in `.github/ci`

1. **Relocate system `git` in `.github/ci/Dockerfile`**:
   - In `python:3.14-slim` (Debian merged-`/usr`, where `/bin` is a symlink to `/usr/bin`), `/usr/bin/git`, `/usr/lib/git-core/git`, and builtin hardlinks `/usr/lib/git-core/git-*` (`git-push`, `git-commit`, `git-receive-pack`, etc.) share the same binary inode.
   - During `docker build`, copy the real ELF binary `/usr/bin/git` to `/usr/lib/git-core/git-real` (off `PATH` and off every standard `git` / `git-*` path).
   - Replace `/usr/bin/git` (which is also `/bin/git`) and every `/usr/lib/git-core/*` file that shared `/usr/bin/git`'s inode with `.github/ci/git`.

2. **System-path router `.github/ci/git`**:
   - Normalizes dashed invocations (`git-push ...` -> `["push", ...]`) when `Path(sys.argv[0]).name` starts with `"git-"`.
   - Resolves the active eval shim directory `prefix`:
     - Checks `os.environ.get("CHIEF_OF_STUFF_PATH_PREFIX")`.
     - If unset in `os.environ` and `os.environ.get("GIT_NO_REPLACE_OBJECTS") != "1"`, walks ancestor processes in `/proc/<pid>/stat` + `/proc/<pid>/environ` while the ancestor `comm` is a shell or `env` (`sh`, `dash`, `bash`, `zsh`, `env`). This catches `env -i /usr/bin/git push` and `env -u CHIEF_OF_STUFF_PATH_PREFIX /usr/bin/git push` inside a fixture shell without affecting `make_repo.build` (no ancestor has `CHIEF_OF_STUFF_PATH_PREFIX`) or `scripts/git_trees.py` (sets `GIT_NO_REPLACE_OBJECTS=1` and spawns directly from `python3`).
   - **When `prefix` is active**:
     - Verifies `shim_git = Path(prefix) / "git"` is an executable file; if missing, fails closed (`sys.exit("git: eval shim missing from CHIEF_OF_STUFF_PATH_PREFIX")`).
     - Loads `_git_read_only` from `shim_git`'s AST (`ast.parse`) so `.github/ci/git` never duplicates or drifts from `evals/run.py`'s allowlist.
     - If `not _git_read_only(argv)` **or** `os.environ.get("GIT_OPTIONAL_LOCKS") != "0"`:
       - Ensures `CHIEF_OF_STUFF_PATH_PREFIX` is exported and `/usr/bin` is present on `PATH`, then `os.execv(shim_git, [str(shim_git), *argv])`.
       - For any disallowed call (`push`, `commit`, alias, unknown flag, etc.), `shim_git` records the call in `calls.jsonl` and exits nonzero (`"git: refused non-allowlisted invocation by eval harness"`).
       - For a direct absolute-path read (`/usr/bin/git status --short` where `GIT_OPTIONAL_LOCKS` is not yet `"0"`), `shim_git` records the call once in `calls.jsonl`, sets `GIT_OPTIONAL_LOCKS=0`, and re-execs `/usr/bin/git`, which on the second hop sees `GIT_OPTIONAL_LOCKS == "0"` and `_git_read_only(argv) == True` and execs `git-real`.
       - For a normal `PATH` read (`git status --short`), `shim_git` already ran first, recorded the call once in `calls.jsonl`, set `GIT_OPTIONAL_LOCKS=0`, and execed `/usr/bin/git`, so `.github/ci/git` immediately execs `git-real` without double-logging.
   - **When `prefix` is not active**:
     - Directly `os.execv`s the real git binary (`/usr/lib/git-core/git-real` in the CI container, or the host's system `git` if `.github/ci/git` is invoked directly in a local unit test).

---

## 3. Step-by-Step Implementation Plan

### Step 1: Create `.github/ci/git` (chmod `0755`)

Create [`.github/ci/git`](file:///Users/locriani/Developer/chief-of-stuff/worktrees/issue-770-plan-1/.github/ci/git) alongside [`.github/ci/agy`](file:///Users/locriani/Developer/chief-of-stuff/worktrees/issue-770-plan-1/.github/ci/agy):

```python
#!/usr/bin/env python3
"""System-path git wrapper for the CI image (#770).

In the CI container, the real git binary lives at /usr/lib/git-core/git-real and every
standard git path (/usr/bin/git, /bin/git, /usr/lib/git-core/git*) points here.
When an eval fixture is active (CHIEF_OF_STUFF_PATH_PREFIX in the environment or an
ancestor shell), invocations are routed through the eval harness's PATH git shim so
every call is recorded in calls.jsonl and non-allowlisted calls are refused.
Outside eval_environment, calls delegate directly to git-real.
"""
import ast
import os
import sys
from pathlib import Path

REAL_GIT_CONTAINER = Path("/usr/lib/git-core/git-real")
SHELL_COMMS = {"sh", "dash", "bash", "zsh", "env"}


def _normalized_argv() -> list[str]:
    name = Path(sys.argv[0]).name
    if name.startswith("git-") and name != "git-real":
        return [name[4:], *sys.argv[1:]]
    return list(sys.argv[1:])


def _ancestor_prefix() -> tuple[str | None, str | None]:
    """Recover CHIEF_OF_STUFF_PATH_PREFIX from an ancestor shell if `env -i` / `env -u` stripped it."""
    if os.environ.get("GIT_NO_REPLACE_OBJECTS") == "1":
        return None, None
    pid = os.getppid()
    visited: set[int] = set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            raw_env = Path(f"/proc/{pid}/environ").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        if comm not in SHELL_COMMS:
            break
        entries: dict[str, str] = {}
        for item in raw_env.split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                entries[k.decode("utf-8", "surrogateescape")] = v.decode("utf-8", "surrogateescape")
        if entries.get("CHIEF_OF_STUFF_PATH_PREFIX"):
            return entries["CHIEF_OF_STUFF_PATH_PREFIX"], entries.get("PATH")
        try:
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
        pid = ppid
    return None, None


def _load_read_only_policy(shim_git: Path):
    source = shim_git.read_text()
    tree = ast.parse(source, filename=str(shim_git))
    mod = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_git_read_only"],
        type_ignores=[],
    )
    ns: dict[str, object] = {}
    exec(compile(mod, str(shim_git), "exec"), ns)
    policy = ns.get("_git_read_only")
    if not callable(policy):
        sys.exit("git: eval shim missing _git_read_only policy")
    return policy


def _find_real_git(prefix: str | None) -> str:
    override = os.environ.get("CHIEF_OF_STUFF_REAL_GIT")
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    if REAL_GIT_CONTAINER.is_file() and os.access(REAL_GIT_CONTAINER, os.X_OK):
        return str(REAL_GIT_CONTAINER)
    me = Path(__file__).resolve()
    shim_dir = Path(prefix).resolve() if prefix else None
    for candidate in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git", "/bin/git"):
        path = Path(candidate)
        if not (path.is_file() and os.access(path, os.X_OK)):
            continue
        resolved = path.resolve()
        if resolved == me or (shim_dir is not None and resolved.parent == shim_dir):
            continue
        return str(path)
    sys.exit("git: real git binary not found")


def main() -> None:
    argv = _normalized_argv()
    prefix = os.environ.get("CHIEF_OF_STUFF_PATH_PREFIX")
    inherited_path = None
    if not prefix:
        prefix, inherited_path = _ancestor_prefix()
    if prefix:
        shim_git = Path(prefix) / "git"
        if not (shim_git.is_file() and os.access(shim_git, os.X_OK)):
            sys.exit("git: eval shim missing from CHIEF_OF_STUFF_PATH_PREFIX")
        read_only = _load_read_only_policy(shim_git)
        if not read_only(argv) or os.environ.get("GIT_OPTIONAL_LOCKS") != "0":
            os.environ["CHIEF_OF_STUFF_PATH_PREFIX"] = prefix
            current_path = os.environ.get("PATH") or inherited_path or "/usr/bin:/bin"
            path_parts = [p for p in current_path.split(os.pathsep) if p]
            if "/usr/bin" not in path_parts:
                path_parts.append("/usr/bin")
            os.environ["PATH"] = os.pathsep.join(path_parts)
            os.execv(shim_git, [str(shim_git), *argv])
    real_git = _find_real_git(prefix)
    os.execv(real_git, [real_git, *argv])


if __name__ == "__main__":
    main()
```

### Step 2: Update `.github/ci/Dockerfile` and Add `.github/ci/run-evals`

1. In [`.github/ci/Dockerfile`](file:///Users/locriani/Developer/chief-of-stuff/worktrees/issue-770-plan-1/.github/ci/Dockerfile), relocate the system `git` binary to `/usr/lib/git-core/git-real` and install `.github/ci/git` at `/usr/bin/git` and every hardlinked `/usr/lib/git-core/git*` builtin sharing `/usr/bin/git`'s inode:
   ```dockerfile
   COPY --chmod=755 .github/ci/git /usr/local/share/chief-of-stuff/ci-git
   RUN cp -p /usr/bin/git /usr/lib/git-core/git-real \
       && for p in $(find /usr/lib/git-core -samefile /usr/bin/git); do \
            ln -f /usr/local/share/chief-of-stuff/ci-git "$p"; \
          done \
       && ln -f /usr/local/share/chief-of-stuff/ci-git /usr/bin/git
   ```
   *(Note: on `origin/develop`, `docker build -t chief-of-stuff-ci -f .github/ci/Dockerfile .` uses `.` as the build context so the `COPY` source is `.github/ci/git`; if built with `.github/ci` as context, use `git` to match `agy`.)*

2. Create [`.github/ci/run-evals`](file:///Users/locriani/Developer/chief-of-stuff/worktrees/issue-770-plan-1/.github/ci/run-evals) (`0755`) as the single `.github/ci` driver for both CI and local developers who want the full container-isolated guarantee:
   ```sh
   #!/bin/sh
   set -eu
   ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
   docker build -t chief-of-stuff-ci -f "$ROOT/.github/ci/Dockerfile" "$ROOT"
   if [ "$#" -eq 0 ]; then
       set -- -m unittest discover -s evals -p "test_*.py"
   fi
   exec docker run --rm --network none -v "$ROOT":/repo:ro chief-of-stuff-ci \
       sh -c 'cp -R /repo/. . && exec python3 -B "$@"' sh "$@"
   ```
   - Calling `.github/ci/run-evals` with no arguments runs the eval unit test suite inside the isolated container.
   - Calling `.github/ci/run-evals evals/run.py ...` runs `python3 -B evals/run.py ...` unchanged inside the isolated container.

3. In [`.github/workflows/tests.yml`](file:///Users/locriani/Developer/chief-of-stuff/worktrees/issue-770-plan-1/.github/workflows/tests.yml), invoke `./.github/ci/run-evals` (or keep the `docker build` + `docker run` steps matching `run-evals`).

### Step 3: Update the Grader Limit Notes (`evals/run.py`, `evals/test_run.py`, `evals/test_cases.py`)

Per Acceptance #2 in the Architect's ruling:

1. In [`_path_git_write`](file:///Users/locriani/Developer/chief-of-stuff/worktrees/issue-770-plan-1/evals/run.py#L369-L380) (`evals/run.py` on `origin/develop`):
   Update the docstring from:
   ```python
   def _path_git_write(_g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
       """Require shim-recorded PATH git argv to match the read allowlist, without parsing shell text.

       An absolute-path git (`/usr/bin/git push`) skips the shim and a call the host denies never reaches
       a shell; neither is graded. Env/config-driven helpers are not graded.
       The fixture lint checks named-remote URLs in an isolated probe, not runtime destinations:
       conditional includes, other destination keys, inherited config and explicit URLs can escape it.
       Runtime isolation and closing the absolute-path coverage gap remain #770 (1.1).
       """
   ```
   to:
   ```python
   def _path_git_write(_g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
       """Require shim-recorded git argv to match the read allowlist, without parsing shell text.

       In CI (and local runs through `.github/ci/run-evals`), standard system git paths (`/usr/bin/git`,
       `/bin/git`, `/usr/lib/git-core/git*`) route through the eval shim, so absolute-path and PATH-reset
       git writes are recorded and refused (#770). Direct local runs of `evals/run.py` outside `.github/ci`
       remain PATH-only, where an absolute-path git skips the shim. A call the host denies before reaching
       a shell and env/config-driven helpers are not graded.
       """
   ```

2. In `PathGitWriteTest` (`evals/test_run.py:529-533` on `origin/develop`):
   Update the class docstring to state that in CI (via `.github/ci`), `/usr/bin/git` and `/bin/git` route through the shim and are recorded and refused, while direct local runs outside `.github/ci` remain `PATH`-only.

3. In `_assert_probe_remote_urls_are_local` (`evals/test_cases.py:208-216` on `origin/develop`):
   Update the final sentence of the docstring (`It does not prove runtime confinement (#770).`) to note that runtime confinement of standard `git` paths holds in CI via `.github/ci` (#770), while this static lint checks fixture remote URLs independently.

### Step 4: Add Red-First Unit Tests in `evals/test_run.py`

In `PathGitWriteTest` (`evals/test_run.py`), add tests that run with **zero skips (`skipped = 0`)** both locally and in the CI container:

1. `test_ci_git_wrapper_records_and_refuses_absolute_path_writes_and_bypasses`:
   - Set up a fixture repo (`make_repo.build`), `calls.jsonl`, and `env = run.eval_environment(root, log, "America/Chicago")`.
   - Create a temporary `system_bin` directory containing symlinks/hardlinks `git` and `git-push` pointing to `run.PLUGIN_ROOT / ".github/ci/git"`, and include `system_bin` after `shims` on `env["PATH"]` so the two-hop delegation exercises `.github/ci/git` on every host.
   - Verify each write/bypass invocation is **refused** (`returncode != 0`, `"refused"` in `stderr`), leaves `origin.git` and the clone unchanged, is **recorded** in `calls.jsonl`, and grades `False` under `run.grade({"type": "path_git_write"}, rec)`:
     - Direct absolute path: `[str(system_bin / "git"), "push"]`
     - Dashed builtin path: `[str(system_bin / "git-push"), "origin", "main"]`
     - `PATH` reset to system bin: `PATH=f"{system_bin}:/usr/bin:/bin"` running `git push`
     - `GIT_OPTIONAL_LOCKS=0` pre-set: `GIT_OPTIONAL_LOCKS=0` with `[str(system_bin / "git"), "push"]`
     - When `/proc/self/environ` is available (Linux / CI container): `/bin/sh -c 'env -u CHIEF_OF_STUFF_PATH_PREFIX "$0" push'` and `/bin/sh -c 'env -i "$0" push'` with `system_bin / "git"`.
   - When running inside the `.github/ci` container (`Path("/usr/lib/git-core/git-real").is_file()`):
     - Also execute literal `["/usr/bin/git", "push"]`, `["/bin/git", "push"]`, and `["/usr/lib/git-core/git-push"]` inside `eval_environment`, asserting each is recorded in `calls.jsonl`, refused (`returncode != 0`, `"refused"` in `stderr`), and graded `False` by `path_git_write`.

2. `test_ci_git_wrapper_allows_and_single_logs_read_only_commands_and_passes_through_outside_eval`:
   - Inside `eval_environment`, run `[str(system_bin / "git"), "status", "--short"]` and `["git", "rev-parse", "HEAD"]` (with `system_bin` immediately after `shims` on `PATH`).
   - Verify both exit `0`, `calls.jsonl` contains **exactly one** entry per command (`[["status", "--short"], ["rev-parse", "HEAD"]]`), and `run.grade({"type": "path_git_write"}, rec)` returns `(True, "only allowlisted PATH git reads")`.
   - Verify that if `{CHIEF_OF_STUFF_PATH_PREFIX}/git` is missing, `.github/ci/git` fails closed (`returncode != 0`, `"eval shim missing"` in `stderr`).
   - Outside `eval_environment` (`CHIEF_OF_STUFF_PATH_PREFIX` unset) and under `scripts/git_trees.py`'s `audit_env()` (`GIT_NO_REPLACE_OBJECTS=1`, `CHIEF_OF_STUFF_PATH_PREFIX` unset), verify `.github/ci/git` delegates to the real `git` binary so `branch -f` / `rev-parse` succeed without touching `calls.jsonl`.

---

## 4. Validation Commands

1. **Local unit test suites** (must report `failed = 0`, `skipped = 0`, `xfailed = 0`):
   ```sh
   python3 -B -m unittest discover -s evals -p "test_*.py"
   python3 -B -m unittest discover -s scripts -p "test_*.py"
   ```
2. **Containerized CI verification** (when Docker is available):
   ```sh
   ./.github/ci/run-evals
   ```

---

## 5. Risks and Controls

| Risk | Control |
| --- | --- |
| Double-recording in `calls.jsonl` when `GIT_SHIM` delegates an allowlisted read to `/usr/bin/git` (`.github/ci/git`) | `GIT_SHIM` sets `GIT_OPTIONAL_LOCKS=0` right before `os.execv` (`evals/run.py:1404`). `.github/ci/git` checks `_git_read_only(argv) and os.environ.get("GIT_OPTIONAL_LOCKS") == "0"` to recognize the second hop and execs `git-real` directly without re-entering `GIT_SHIM`. Disallowed calls (`not _git_read_only(argv)`) always route to `GIT_SHIM` even if `GIT_OPTIONAL_LOCKS=0` is set by the caller. |
| Git helper binaries (`git-receive-pack`, `git-upload-pack`, `git-push`) in `/usr/lib/git-core` share `/usr/bin/git`'s inode in Debian | `.github/ci/Dockerfile` replaces all `/usr/lib/git-core` hardlinks to `/usr/bin/git` with `.github/ci/git`, and `.github/ci/git` normalizes `argv[0]` names starting with `git-` (`git-X ...` -> `["X", ...]`) so both `make_repo.build` pushes outside `eval_environment` and `git-push` refusals inside `eval_environment` work identically. |
| Trusted harness/engine helpers (`scripts/git_trees.py`) strip `CHIEF_OF_STUFF_PATH_PREFIX` to inspect worktrees | `git_trees.py` sets `GIT_NO_REPLACE_OBJECTS=1` and spawns `git` directly from `python3` (not a shell). `.github/ci/git` skips `/proc` ancestor recovery when `GIT_NO_REPLACE_OBJECTS=1` or when the parent process is not in `SHELL_COMMS`. |
| Policy drift between `evals/run.py` and `.github/ci/git` | `.github/ci/git` loads `_git_read_only` directly from `{CHIEF_OF_STUFF_PATH_PREFIX}/git` via `ast.parse`, keeping `evals/run.py` as the single source of truth. |
| Stack-independence (`ARCHITECTURE.md` §11) | `evals/run.py` contains no references to Docker, containers, or `.github/ci`; all isolation wiring lives under `.github/ci/`. |

---

## 6. File Ownership

| Path | Ownership | Purpose |
| --- | --- | --- |
| `docs/plans/issue-770.md` | `write` | Implementation plan for issue #770 (plan stage write scope) |
| `.github/ci/git` | `write` (implement stage) | System-path `git` wrapper routing eval fixture calls through `{CHIEF_OF_STUFF_PATH_PREFIX}/git` |
| `.github/ci/Dockerfile` | `write` (implement stage) | Relocate `/usr/bin/git` to `/usr/lib/git-core/git-real` and install `.github/ci/git` at `/usr/bin/git` and `/usr/lib/git-core/git*` |
| `.github/ci/run-evals` | `write` (implement stage) | Executable CI/local driver to build `chief-of-stuff-ci` and run tests or `evals/run.py` in the isolated container |
| `.github/workflows/tests.yml` | `write` (implement stage) | Invoke `.github/ci/run-evals` in CI |
| `evals/run.py` | `write` (implement stage) | Update `_path_git_write` docstring limit note |
| `evals/test_run.py` | `write` (implement stage) | Update `PathGitWriteTest` docstring and add red-first tests for `.github/ci/git` and `/usr/bin/git` / `/bin/git` |
| `evals/test_cases.py` | `write` (implement stage) | Update `_assert_probe_remote_urls_are_local` docstring limit note |
