#!/usr/bin/env python3
"""Remove finished worktrees, and the build output in them, only past every finding-27 guard (1.0 plan M3 item 5, E19).

Preview by default: each tree under the `Worktrees:` dir is listed as one to remove, or kept and why. `--remove`
removes, and logs each removal and each refusal on the owning store items. A tree is removed only when every guard is
verified:

- owner: it is a linked worktree, the store items that own it are all finished (closed on the forge), and no live
  worker claims it or process has its cwd inside it. A registered worker whose PID was reused is gone; one that
  cannot be verified is not;
- `git status --porcelain --ignored` lists nothing but build output or engine metadata (`tree_state.unsaved`);
- no index flags hide tracked files from git status;
- no in-progress Git operation or initialized submodule metadata remains (submodule contents are never inspected);
- no container binds a path inside it;
- nothing on it is unpushed or unmerged (`tree_state.at_risk`).

A guard that cannot be verified keeps the tree. Worker-written repositories are read only through git_view, and a tree
is removed by deleting it and then its admin dir, so no git command runs on its config. Engine code only: the model and
workers never remove a tree (B10).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

import git_view  # noqa: E402
from git_trees import audit_env, clean, discover, git, read_state, trusted_commons  # noqa: E402
from one_shot import launcher_copy, trees_lock  # noqa: E402
from process_status import PIDFILE, check, one_shot_runs, registrations  # noqa: E402
from store import Store, WorkItem  # noqa: E402
from tree_state import at_risk, unsaved  # noqa: E402
from settings import SettingsError, load as load_settings  # noqa: E402
from workspace import settings_path, worktrees_dir  # noqa: E402

FINISHED = "closed"  # the forge state of a finished item
CONTAINER_CLIS = ("docker", "podman")
TIMEOUT = 15


def containers() -> list[Path] | None:
    """The host path of every mount of every container, running or stopped; None when an installed runtime cannot say."""
    found: list[Path] = []
    for cli in CONTAINER_CLIS:
        exe = shutil.which(cli)
        if exe is None:
            continue  # not installed, so none of its containers exist
        try:
            ids = subprocess.run([exe, "ps", "-aq"], capture_output=True, text=True, timeout=TIMEOUT)
            if ids.returncode:
                return None
            if not ids.stdout.split():
                continue
            mounts = subprocess.run([exe, "inspect", "--format", "{{range .Mounts}}{{.Source}}\n{{end}}", *ids.stdout.split()],
                                    capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        if mounts.returncode:
            return None
        found += [Path(line.strip()) for line in mounts.stdout.splitlines() if line.strip()]
    return found


def git_guards(tree: Path, expected_common: Path | None = None) -> list[str]:
    """What git status, index flags and the unlanded-work rule say losing this tree loses, read through one view."""
    try:
        with git_view.opened(tree, audit_env(), expected_common=expected_common) as view:
            code, status = git(["status", "--porcelain", "--ignored"], tree, view)
            flags_code, _ = git(["ls-files", "-v"], tree, view)
            state = read_state(tree, view)
    except git_view.CacheError as exc:
        return [f"git view cache configuration error: {exc}"]
    except (git_view.Unviewable, RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return ["git cannot read this tree"]
    if code:
        return ["git status --ignored is unreadable"]
    if flags_code:
        return ["git ls-files -v is unreadable"]
    lost = unsaved(status)
    out = [f"{len(lost)} unsaved by git status --ignored ({', '.join(lost[:3])})"] if lost else []
    hidden = view.hidden_index_entries
    if hidden:
        out.append(f"{hidden} files hidden from git status by index flags")
    return out + list(at_risk(state))


def inside(path: Path, tree: Path) -> bool:
    return path == tree or path.is_relative_to(tree)


def binds(mounts: list[Path], tree: Path) -> bool:
    return any(inside(m, tree) or inside(tree, m) for m in (mount.resolve() for mount in mounts))


def processes() -> list[tuple[int, Path]] | None:
    """The resolved cwd of every live process; None when lsof cannot say."""
    found: list[tuple[int, Path]] = []
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn"],
                                capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return []
        if result.returncode:
            return None
        pid = None
        for line in result.stdout.splitlines():
            if line.startswith("p") and pid is None:
                pid = int(line[1:])
                if pid <= 0:
                    return None
            elif line == "fcwd" and pid is not None:
                continue  # lsof includes the descriptor even with -Fpn
            elif line.startswith("n") and pid is not None and Path(line[1:]).is_absolute():
                found.append((pid, Path(line[1:]).resolve()))
                pid = None
            else:
                return None
        if pid is not None:
            return None
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
        return None
    return found


def claims(root: Path, trees: Path, tree: Path) -> list[str]:
    """Why a worker's claim keeps `tree`, read fresh: a registered worker that is running or cannot be verified (a
    reused PID is gone), an unreadable registry entry, a process in the tree, a live one-shot pid file, or one still
    awaiting reconciliation."""
    regs = registrations(root)
    out = [] if len(list((root / ".chief-of-stuff" / "sessions").glob("*.json"))) == len(regs) else [
        "the worker registry has an unreadable entry"]
    mine = {pid: record for pid, record in regs.items() if (root / record["worktree"]).resolve() == tree}
    status = {row["pid"]: row["status"] for row in check(root, list(mine))} if mine else {}
    for pid, record in mine.items():
        state = status.get(pid, "unverified")
        if state == "running":
            out.append(f"live worker {record['name']} (pid {pid})")
        elif state not in ("gone", "pid_reused"):
            out.append(f"worker {record['name']} (pid {pid}) cannot be verified")
    current = processes()
    if current is None:
        out.append("open processes cannot be checked")
    elif any(pid != os.getpid() and inside(cwd, tree) for pid, cwd in current):
        out.append("live process in tree")
    # Adoption reads a dead worker's dispatch, result and logs until this run's launcher report exists. Do not parse
    # the worker-written pid file here: an unreadable claim must also keep its evidence, without scanning trackers.
    pidfile = tree / PIDFILE
    if pidfile.exists():
        try:
            reconciled = launcher_copy(root, tree).stat().st_mtime_ns > pidfile.stat().st_mtime_ns
        except OSError:
            reconciled = False
        if not reconciled:
            out.append("one-shot awaiting reconciliation")
    return out + [f"live one-shot (pid {pid})" for path, pid, _ in one_shot_runs(trees) if path.resolve() == tree]


def owned_items(root: Path, db: Store) -> list[tuple[Path, WorkItem]]:
    return [((root / row.path).resolve(), item) for item in db.items() for row in db.ownership(item.issue_key)]


def owner_guards(tree: Path, owned: list[tuple[Path, WorkItem]],
                 completed: set[str] | frozenset[str] = frozenset()) -> tuple[list[WorkItem], list[str]]:
    items = list({item.issue_key: item for path, item in owned if inside(path, tree)}.values())
    kept = [] if items else ["no store item owns it"]
    kept += [f"{item.issue_key} is not finished" for item in items if item.forge_state != FINISHED]
    kept += [f"{item.issue_key}: merged, awaiting main CI" for item in items
             if item.merge_state == "merged" and item.issue_key not in completed]
    return items, kept


def layout_guards(tree: Path, expected_common: Path | None = None) -> tuple[git_view.Layout | None, list[str]]:
    """Re-locate the tree and check that its linked-worktree metadata permits removal."""
    try:
        layout = git_view.locate(tree, expected_common=expected_common)
    except git_view.Unviewable:
        return None, ["git cannot read this tree"]
    if layout.tree is None or layout.gitdir == layout.common:
        return layout, ["not a linked worktree"]
    if (layout.gitdir / "locked").exists():
        return layout, ["locked"]
    try:
        for entry in layout.gitdir.iterdir():
            if entry.name in ("BISECT_LOG", "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
                return layout, [f"git operation in progress ({entry.name})"]
    except OSError:
        return layout, ["git operations cannot be checked"]
    try:
        if next((layout.gitdir / "modules").iterdir(), None) is not None:
            return layout, ["initialized submodules"]
    except FileNotFoundError:
        pass
    except OSError:
        return layout, ["submodules cannot be checked"]
    return layout, []


def execute(root: Path, trees: Path | None, db: Store, *, remove: bool = False,
            completed: set[str] | frozenset[str] = frozenset()) -> int:
    """Clean using all safety guards; merged owners also need this pass's verified completions.

    Without completion evidence (including the CLI), merged trees are retained. None means no Worktrees dir.
    """
    trees_dir = str(trees) if trees is not None else ""
    trees = trees if trees is not None else root
    settings = load_settings(root, settings_path((root / "CLAUDE.md").read_text()))
    commons = trusted_commons([root / path for path in settings.repos.values()])
    owned = owned_items(root, db)
    mounts = containers()
    failed = False
    for tree in discover(root, trees_dir):
        common = commons.get(tree)
        layout, kept = layout_guards(tree, common)
        partial: list[str] = []
        items, owners_kept = owner_guards(tree, owned, completed)
        kept += owners_kept
        kept += claims(root, trees, tree)
        if mounts is None:
            kept.append("containers cannot be checked")
        elif binds(mounts, tree):
            kept.append("a container mount overlaps it")
        if layout is not None and layout.tree is not None:
            kept += git_guards(tree, common)
        if not kept and remove:
            removal_started, tree_removed = False, False
            try:
                with trees_lock(trees):  # workers register under this lock: re-check, then delete, holding it
                    layout, kept = layout_guards(tree, common)
                    kept += claims(root, trees, tree)
                    if layout is not None and layout.tree is not None:
                        kept += git_guards(tree, common)
                    current_mounts = containers()
                    if current_mounts is None:
                        kept.append("containers cannot be checked")
                    elif binds(current_mounts, tree):
                        kept.append("a container mount overlaps it")
                    try:
                        items, owners_kept = owner_guards(tree, owned_items(root, db), completed)
                        kept += owners_kept
                    except SQLAlchemyError as exc:
                        failed = True
                        kept.append(f"store ownership cannot be checked ({type(exc).__name__})")
                    if not kept:
                        removal_started = True
                        shutil.rmtree(tree)
                        tree_removed = True
                        shutil.rmtree(layout.gitdir)  # what `git worktree prune` would delete, without running git on its config
            except OSError as exc:
                failed, kept = True, [f"removal failed ({type(exc).__name__})"]
                if removal_started:
                    if not tree_removed:
                        partial.append(f"tree remains ({tree})")
                    partial.append(f"admin dir remains ({layout.gitdir})")
        keys = ", ".join(item.issue_key for item in items)
        verb = "keep" if kept else "removed" if remove else "remove"
        line = clean(f"partially removed {tree.name}: {'; '.join(partial + kept)}" if partial else
                     f"{verb} {tree.name} — {'; '.join(kept) if kept else keys}")
        print(line)
        if remove:
            at = db.clock().isoformat()
            for item in items:
                try:
                    # Only an unchanged keep repeats each pass; every removal is its own event.
                    last = next((entry.message for entry in reversed(db.log(item.issue_key))
                                 if entry.message.startswith(("keep ", "removed ", "partially removed "))), None)
                    if not (line.startswith("keep ") and last == line):
                        db.add_log(item.issue_key, line, at)
                except SQLAlchemyError as exc:
                    failed = True
                    print(f"cannot log cleanup result for {item.issue_key} ({type(exc).__name__})", file=sys.stderr)
    if not remove:
        print("preview: nothing removed; --remove removes the trees marked remove")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True, help="the workspace whose Worktrees dir is cleaned")
    ap.add_argument("--store", type=Path, required=True, help="the engine's store")
    ap.add_argument("--remove", action="store_true", help="remove the trees the preview marks; without it nothing is removed")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if not args.store.is_file():  # Store() would create an empty one
        print(f"no store at {args.store}: nothing removed", file=sys.stderr)
        return 1
    db = None
    try:
        trees_dir = worktrees_dir((root / "CLAUDE.md").read_text())
        db = Store(args.store)
        return execute(root, root / trees_dir if trees_dir else None, db, remove=args.remove)
    except (OSError, UnicodeDecodeError, SQLAlchemyError, SettingsError) as exc:
        print(f"cannot read the workspace or the store ({type(exc).__name__}): nothing removed", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
