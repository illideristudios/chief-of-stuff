# Issue #827: Workspace policy overlay: per-workspace keys over the shipped policy

Implementation plan for `github:illideristudios/chief-of-stuff#827`. This plan stage changes only `docs/plans/issue-827.md` and no runtime behavior. Line numbers below refer to `origin/develop` at `4417a8fe`. This plan worktree (`work/issue-827-plan-1`) is branched from `50091f80` (`origin/main`), which predates `scripts/policy.py`, `scripts/engine.py`, `scripts/reconcile.py`, and `assets/policy.toml`; cut the implementation branch from `origin/develop`, never from `50091f80`.

```
O := the workspace settings name [policy] overlay = "<path>"
R := <path> is relative to the workspace root and resolves inside it
F := the overlay file at <root>/<path> exists and is valid UTF-8 TOML
K := every section, named entry, and key in the overlay exists in the shipped policy schema
P := the workspace runs the shipped policy with the overlay's keys merged over it
S := the workspace runs the unmerged shipped policy (assets/policy.toml)
Δ := the merged policy's SHA-256 fingerprint differs from .chief-of-stuff/policy.fingerprint
L := the engine logs a policy-change entry and records the new fingerprint

1. □(¬O → S)                           [no [policy] overlay runs assets/policy.toml]
2. □((O ∧ R ∧ F ∧ K) → P)              [policy.overlay merges table keys; fields replace whole]
3. □((O ∧ ¬(R ∧ F ∧ K)) → (¬P ∧ ¬S))   [missing/outside file or unknown section/key is refused, never fallback]
4. □(Δ → L)                            [fingerprint change is logged once per workspace]
∴ each workspace runs a known policy (its merged overlay or the shipped policy) or refuses to start
```

When a workspace sets `[policy] overlay = "<path>"` in its settings TOML, `settings.workspace_policy` merges that TOML's keys over `assets/policy.toml` via `policy.overlay`. Setting `[stages.review.policy] architect = false` in that workspace's overlay disables the review stage's architect gate (`reconcile.py:310-316`) for that workspace alone without altering any other workspace or the shipped policy. Any missing file, path outside the workspace root, or unknown section or key is refused by name.

## State on `origin/develop` (`4417a8fe`)

PR `#881` (`loop/827`, commits `7e1eb2b1`..`cc203fb1`, merged at `a9d66bb7`) already landed the core implementation of `#827` on `origin/develop`:

| Component | Location on `origin/develop` | Current behavior |
|---|---|---|
| Pure overlay merge (Entities) | `scripts/policy.py:314-341` (`_NAMED`, `overlay`) | Deep-copies `shipped`, replaces `default_workflow`, merges `[policy]` and keys/nested `policy` tables of existing `[personas.<name>]`, `[stages.<name>]`, `[rooms.<name>]`, and `[workflows.<name>]` entries. Refuses any unknown top-level section with `PolicyError(f"{where}: unknown section [{section}]")` and any unknown named table with `PolicyError(f"{where}: unknown section [{section}.{name}]")`. `policy.parse` (`:344-361`) then refuses any unknown key with its name. |
| Settings `[policy] overlay` & shared loader | `scripts/settings.py:222, 534-543, 583, 598-623` (`Settings.policy_overlay`, `_policy_overlay`, `_policy_toml`, `workspace_policy_data`, `workspace_policy`) | Validates `[policy] overlay = "<path>"` inside `root`, refuses unknown keys in `[policy]`, refuses a missing overlay file in `workspace_policy_data` with `PolicyError(f"[policy] overlay {overlay}: no such file")`, and parses the merged policy in `workspace_policy(root, overlay)`. |
| Engine loader & fingerprint log | `scripts/engine.py:1022-1035, 1116-1117` (`_note_policy`, `main`) | Loads `policies = settings.workspace_policy(root, config.policy_overlay)` and calls `_note_policy(root, settings.workspace_policy_data(root, config.policy_overlay), config.policy_overlay)`, which hashes the canonical JSON (`hashlib.sha256(...).hexdigest()[:12]`), compares it against `supervisor.state_dir(root) / "policy.fingerprint"`, and prints `engine: policy changed: <fingerprint> (overlay <path>\|shipped policy)` to `sys.stderr` when changed. Because `root` and `supervisor.state_dir(root)` are per-workspace, each child workspace under §12 (`#825`) uses its own overlay and fingerprint file. |
| `init` and `pages` shared loader | `scripts/init_workspace.py:135-141, 248`; `scripts/pages.py:421-425, 443` | `_require_engine_ready` and `pages.Handler` load the workspace's merged policy through `settings.workspace_policy`. |
| User documentation | `README.md:148-161` | Documents `## Workspace policy` and the truth checksum. |
| Unit tests | `evals/test_policy.py:357-387` (`OverlayTest`), `evals/test_settings.py:370-411` (`PolicyOverlayTest`), `evals/test_engine.py:3332-3348` (`PolicyFingerprintTest`) | Cover stage policy key merging, whole-field replacement, unknown section/key refusal, missing overlay file refusal, and single-emission fingerprint logging. |

## Remaining gaps to close on `origin/develop`

Inspection of `a9d66bb7` on `origin/develop` identified three small gaps to close so every acceptance clause and boundary holds without exception:

1. **Absolute overlay paths inside `root` and direct-call path traversal (`scripts/settings.py:534-543, 609-618`):**
   - In Python `pathlib`, `Path(root) / "/abs/path"` discards `root`. If `/abs/path` happens to point inside `root`, `(Path(root) / path).resolve().is_relative_to(Path(root).resolve())` returns `True` even though `path` is not relative to the workspace root.
   - `workspace_policy_data(root, overlay)` checks `path.is_file()` without checking `Path(overlay).is_absolute()` or `path.resolve().is_relative_to(Path(root).resolve())`, so a caller invoking `workspace_policy(root, "../outside.toml")` or following a symlink that resolves outside `root` bypasses `_policy_overlay`'s root check if `settings.load` was not the source or if the symlink target changed after `settings.load`.
2. **Uncaught `PolicyError` in `init_workspace.py` (`scripts/init_workspace.py:135-141, 244-251`):**
   - When `init` runs on a workspace with preserved `chief-of-stuff.toml` whose `[policy] overlay` file is missing or contains an unknown section/key, `_require_engine_ready(existing_settings, root)` calls `workspace_policy(root, config.policy_overlay)`, which raises `policy.PolicyError`.
   - `init_workspace.main` catches `SettingsError` around `load_settings` and `(InitError, OSError)` at the top level (`:286`). Because `PolicyError` subclasses `ValueError` rather than `InitError`, `init_workspace.main` crashes with an unhandled traceback instead of printing `refused: ...` and returning `1`.
3. **Planner-level assertion for `architect = false` in `evals/test_settings.py` (`:382-388`) and misplaced `unittest.main()` (`:412`):**
   - `PolicyOverlayTest.test_architect_false_in_an_overlay_stops_architect_requests_for_that_workspace_only` asserts `.resolve(stage="review").architect` is `False` vs `True`, but does not pass the two workspace policies into `reconcile.plan` (`scripts/reconcile.py:310-316`) to verify that the workspace with `architect = false` advances to `Merge` while the other workspace waits with `"architect consultation is required"`.
   - `if __name__ == "__main__": unittest.main()` in `evals/test_settings.py` was inserted at line 412 in the middle of the file, above `KanbanSettingsTest`..`WorkflowStagesTest`.

## Implementation

1. **Cut the implementation branch from `origin/develop` (`4417a8fe`).** Keep all existing `#881` behavior intact.
2. **In `scripts/settings.py`:**
   - In `_policy_overlay(table, root: Path) -> str` (`:534-543`), reject `Path(path).is_absolute()` alongside paths resolving outside `root`:
     ```python
     if Path(path).is_absolute() or not (Path(root) / path).resolve().is_relative_to(Path(root).resolve()):
         raise SettingsError(f"[policy] overlay: {path!r} is outside the workspace root")
     ```
   - In `workspace_policy_data(root: Path, overlay: str = "") -> dict` (`:609-618`), enforce the same workspace-root boundary before checking `path.is_file()`:
     ```python
     path = Path(root) / overlay
     if Path(overlay).is_absolute() or not path.resolve().is_relative_to(Path(root).resolve()):
         raise policy.PolicyError(f"[policy] overlay {overlay}: outside the workspace root")
     if not path.is_file():
         raise policy.PolicyError(f"[policy] overlay {overlay}: no such file")
     ```
3. **In `scripts/init_workspace.py`:**
   - Import `PolicyError` from `policy` (or re-export via `settings`) and wrap `workspace_policy(root, config.policy_overlay)` in `_require_engine_ready` (`:135-141`):
     ```python
     try:
         policies = workspace_policy(root, config.policy_overlay)
     except PolicyError as exc:
         raise InitError(f"existing settings are invalid: {exc}") from None
     unmapped = unmapped_stages(config.kanban, policies)
     ```
4. **In `evals/test_settings.py` and `evals/test_init_workspace.py`:**
   - Extend `PolicyOverlayTest.test_architect_false_in_an_overlay_stops_architect_requests_for_that_workspace_only` to call `reconcile.plan` on a reviewed, merge-ready item in stage `"merge"` (with a completed `"review"` segment and `open_findings={"github:o/r#1": {"critical": 0, "important": 0, "minor": 0}}`) under both workspace policies, asserting `mine` returns `[reconcile.Merge("github:o/r#1")]` and `other` returns `[reconcile.Wait("github:o/r#1", "architect consultation is required")]`.
   - Extend `PolicyOverlayTest.test_the_policy_table_takes_only_an_overlay_inside_the_workspace` to assert that an absolute path inside `root` (`f'[policy]\noverlay = "{root / "policy.toml"}"\n'`) raises `st.SettingsError` and that `st.workspace_policy(root, "../elsewhere.toml")` raises `st.policy.PolicyError`.
   - Move `PolicyOverlayTest` and `if __name__ == "__main__": unittest.main()` to the bottom of `evals/test_settings.py`.
   - In `evals/test_init_workspace.py` (`InitWorkspaceTest`), add `test_preserved_settings_with_a_missing_or_invalid_policy_overlay_refuse`: write `_engine_ready_kanban(["00 - PLAN"], "HOLD") + '\n[policy]\noverlay = "missing.toml"\n'` to `chief-of-stuff.toml`, run `self.run_init()`, and assert exit code `1` with `"missing.toml"` in `err`.

## Tests (TDD)

1. On `origin/develop`, add the new assertions in `evals/test_settings.py` and `evals/test_init_workspace.py` first and run:
   - `python3 -m unittest evals.test_settings evals.test_init_workspace`
   Confirm the new absolute-path/direct-traversal and `init` missing-overlay tests fail before editing `scripts/settings.py` and `scripts/init_workspace.py`.
2. Apply the changes in `scripts/settings.py` and `scripts/init_workspace.py`, then run:
   - `python3 -m unittest evals.test_policy evals.test_settings evals.test_init_workspace evals.test_engine evals.test_architecture`
   - `python3 -m unittest discover -s evals -p 'test_*.py'`
   Report the exact `passed`, `failed`, `skipped`, and `xfailed` counts from the full suite run.

### Baseline verification in this plan worktree

In this `50091f80` worktree, `python3 -B -m unittest evals.test_settings evals.test_init_workspace evals.test_architecture` ran **80 tests in 3.846s: 80 passed, 0 failed, 0 skipped, 0 xfailed**.

## Risks

- **Branch base (`50091f80` vs `origin/develop`):** This plan worktree is at `50091f80` (`origin/main`), where `scripts/policy.py`, `scripts/engine.py`, and `assets/policy.toml` do not exist. Branching the implementation from `50091f80` instead of `origin/develop` would miss `#881` and the entire 1.0 engine.
- **Interaction with `#706` (`policy.d` overlay directory):** `#706` folds `.chief-of-stuff/policy.d/*.toml` on top of `workspace_policy_data(root, overlay)`. Keeping `policy.overlay(shipped, over, where)` and `workspace_policy_data(root, overlay)`'s signatures and return types unchanged preserves `#706`'s seam.
- **Interaction with `#825` (§12 parent and child workspaces):** Each child workspace has its own `root`, `Settings.policy_overlay`, and `supervisor.state_dir(child_root) / "policy.fingerprint"`. Passing `root` and `config.policy_overlay` explicitly into `settings.workspace_policy` and `engine._note_policy` ensures child workspaces never share or clobber overlays or fingerprints.
- **Architecture docs ownership:** Per `CLAUDE.md`, `architecture/ARCHITECTURE.md` and `architecture/compliance.md` belong exclusively to `frank-lloyd-aight`; do not touch them.

## Proposed file ownership

The issue's `## File ownership` lists `scripts/settings.py`, `scripts/policy.py`, `scripts/engine.py`, `evals/test_settings.py`, `evals/test_policy.py`, and `README.md`. Add `scripts/init_workspace.py`, `evals/test_init_workspace.py`, and `evals/test_engine.py` to `write` so the shared `init` loader refusal and engine fingerprint test are within write scope:

- write: `scripts/settings.py`
- write: `scripts/policy.py`
- write: `scripts/engine.py`
- write: `scripts/init_workspace.py`
- write: `evals/test_settings.py`
- write: `evals/test_policy.py`
- write: `evals/test_engine.py`
- write: `evals/test_init_workspace.py`
- write: `README.md`
- read: `assets/policy.toml`
- read: `scripts/reconcile.py`
- read: `scripts/pages.py`
- read: `scripts/supervisor.py`
- read: `architecture/ARCHITECTURE.md`
- read: `docs/plans/issue-827.md`
