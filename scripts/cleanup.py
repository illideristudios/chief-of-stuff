#!/usr/bin/env python3
"""Remove finished worktrees, and the build output in them, only past every finding-27 guard (1.0 plan M3 item 5, E19).

Preview by default: each tree under the `Worktrees:` dir is listed as one to remove, or kept and why. `--remove`
removes, and logs each removal and each refusal on the owning store items. A tree is removed only when every guard is
verified:

- owner: it is a linked worktree, the store items that own it are all finished (closed on the forge), and no live
  worker claims it. A registered worker whose PID was reused is gone; one that cannot be verified is not;
- `git status --porcelain --ignored` lists nothing but build output (`tree_state.unsaved`);
- no container binds a path inside it;
- nothing on it is unpushed or unmerged (`tree_state.at_risk`).

A guard that cannot be verified keeps the tree. Worker-written repositories are read only through git_view, and a tree
is removed by deleting it and then its admin dir, so no git command runs on its config. Engine code only: the model and
workers never remove a tree (B10).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

import git_view  # noqa: E402
from git_trees import audit_env, clean, discover, git, read_state  # noqa: E402
from one_shot import trees_lock  # noqa: E402
from process_status import check, one_shot_runs, registrations  # noqa: E402
from store import Store  # noqa: E402
from tree_state import at_risk, unsaved  # noqa: E402
from workspace import worktrees_dir  # noqa: E402

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


def git_guards(tree: Path) -> list[str]:
    """What `git status --ignored` and the unlanded-work rule say losing this tree loses, read through one view."""
    try:
        with git_view.opened(tree, audit_env()) as view:
            code, status = git(["status", "--porcelain", "--ignored"], tree, view)
            state = read_state(tree, view)
    except (git_view.Unviewable, RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return ["git cannot read this tree"]
    if code:
        return ["git status --ignored is unreadable"]
    lost = unsaved(status)
    out = [f"{len(lost)} unsaved by git status --ignored ({', '.join(lost[:3])})"] if lost else []
    return out + list(at_risk(state))


def inside(path: Path, tree: Path) -> bool:
    return path == tree or path.is_relative_to(tree)


def claims(root: Path, trees: Path, tree: Path) -> list[str]:
    """Why a worker's claim keeps `tree`, read fresh: a registered worker that is running or cannot be verified (a
    reused PID is gone), an unreadable registry entry, or a live one-shot pid file."""
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
    return out + [f"live one-shot (pid {pid})" for path, pid, _ in one_shot_runs(trees) if path.resolve() == tree]


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
    try:
        trees_dir = worktrees_dir((root / "CLAUDE.md").read_text())
        db = Store(args.store)
        owned = [((root / row.path).resolve(), item) for item in db.items() for row in db.ownership(item.issue_key)]
    except (OSError, UnicodeDecodeError, SQLAlchemyError) as exc:
        print(f"cannot read the workspace or the store ({type(exc).__name__}): nothing removed", file=sys.stderr)
        return 1
    trees = root / trees_dir
    mounts = containers()
    failed = False
    for tree in discover(root, trees_dir):
        kept: list[str] = []
        try:
            layout = git_view.locate(tree)
        except git_view.Unviewable:
            layout, kept = None, ["git cannot read this tree"]
        if layout is not None and (layout.tree is None or layout.gitdir == layout.common):
            kept.append("not a linked worktree")
        elif layout is not None and (layout.gitdir / "locked").exists():
            kept.append("locked")
        items = list({item.issue_key: item for path, item in owned if inside(path, tree)}.values())
        if not items:
            kept.append("no store item owns it")
        kept += [f"{item.issue_key} is not finished" for item in items if item.forge_state != FINISHED]
        kept += claims(root, trees, tree)
        if mounts is None:
            kept.append("containers cannot be checked")
        elif any(inside(mount.resolve(), tree) for mount in mounts):
            kept.append("a container binds a path in it")
        if layout is not None and layout.tree is not None:
            kept += git_guards(tree)
        if not kept and args.remove:
            try:
                with trees_lock(trees):  # a one-shot claims its tree under this lock: re-check, then delete, holding it
                    kept = claims(root, trees, tree) + git_guards(tree)
                    if not kept:
                        shutil.rmtree(tree)
                        shutil.rmtree(layout.gitdir)  # what `git worktree prune` would delete, without running git on its config
            except OSError as exc:
                failed, kept = True, [f"removal failed ({type(exc).__name__})"]
        keys = ", ".join(item.issue_key for item in items)
        verb = "keep" if kept else "removed" if args.remove else "remove"
        line = clean(f"{verb} {tree.name} — {'; '.join(kept) if kept else keys}")
        print(line)
        if args.remove:
            at = datetime.now(timezone.utc).isoformat()  # shortcut: wall clock, the engine's injected clock replaces it in M3
            for item in items:
                db.add_log(item.issue_key, line, at)
    if not args.remove:
        print("preview: nothing removed; --remove removes the trees marked remove")
    db.engine.dispose()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
