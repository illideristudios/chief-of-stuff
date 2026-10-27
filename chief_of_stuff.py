#!/usr/bin/env python3
"""One installed command for chief-of-stuff setup and operation.

`python3 chief_of_stuff.py install` copies a versioned release to the user's data directory
and writes a stable, tiny launcher to /usr/local/bin. Later installs only move the user-owned
`current` pointer when that launcher is already correct.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import start_coordinator
from scripts._vendor.toon_format import encode as toon_encode

SOURCE = Path(__file__).resolve().parent
DEFAULT_INSTALL_DIR = Path.home() / ".local" / "share" / "chief-of-stuff"
DEFAULT_BIN_DIR = Path("/usr/local/bin")
COMMANDS = {
    "start": "start_coordinator.py",
    "init": "scripts/init_workspace.py",
    "work": "scripts/work.py",
    "engine": "scripts/engine.py",
    "supervisor": "scripts/supervisor.py",
    "models": "scripts/install_model_guidance.py",
    "worktree": "scripts/make_worktree.py",
    "worker": "scripts/spawn_session.py",
    "audit": "scripts/audit_tasks.py",
    "processes": "scripts/process_status.py",
    "result": "scripts/one_shot_result.py",
    "adopt": "scripts/adopt_one_shots.py",
    "board": "scripts/pages.py",
    "inbox": "scripts/inbox.py",
    "backlog": "scripts/backlog.py",
    "kanban": "scripts/kanban_cli.py",
    "pipeline": "scripts/pipeline_cli.py",
    "notify": "scripts/notify_service.py",
    "health": "scripts/probe_health.py",
    "pages": "scripts/pages.py",
    "decision": "scripts/decision_page.py",
    "log": "scripts/tracker_write.py",
    "tracker": "scripts/tracker_read.py",
    "merge-approved": "scripts/merge_approved.py",
    "merge-ready": "scripts/merge_ready.py",
    "performance": "scripts/performance_report.py",
    "limits": "scripts/limits_cli.py",
    "review-threads": "scripts/review_threads.py",
    "sources": "scripts/board_sources.py",
    "tick": "scripts/flow.py",
}
# Commands whose scripts import store (SQLAlchemy/Alembic).
STORE_BACKED = frozenset({"adopt", "board", "decision", "engine", "limits", "notify", "pages", "performance", "tick", "work", "worker"})


def wrapper_text(current: Path) -> str:
    """No shell, project import, or arbitrary executable path in the system-owned shim."""
    entry = str(current / "chief_of_stuff.py")
    versions = str((current.parent / "versions").resolve())
    return ("#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"entry = {entry!r}\n"
            "pinned = os.environ.get('CHIEF_OF_STUFF_RELEASE')\n"
            "if pinned:\n"
            "    release = Path(pinned).resolve()\n"
            f"    if str(release.parent) != {versions!r}:\n"
            "        raise SystemExit('invalid pinned chief-of-stuff release')\n"
            "    entry = str(release / 'chief_of_stuff.py')\n"
            "if not os.path.isfile(entry):\n"
            "    raise SystemExit('chief-of-stuff release is missing; reinstall from the checkout')\n"
            "os.execv(sys.executable, [sys.executable, entry, *sys.argv[1:]])\n")


def install(*, source: Path, install_dir: Path, bin_dir: Path, dry_run: bool = False,
            replace_wrapper: bool = False) -> dict:
    install_dir, bin_dir = install_dir.expanduser().absolute(), bin_dir.expanduser().absolute()
    current = install_dir / "current"
    binary = bin_dir / "chief-of-stuff"
    expected = wrapper_text(current)
    if binary.is_symlink() or (binary.exists() and not binary.is_file()):
        raise ValueError(f"{binary} already exists and is not a regular launcher file")
    if binary.exists() and binary.read_text() != expected and not replace_wrapper:
        raise ValueError(f"{binary} differs from this launcher; inspect it, then pass --replace-wrapper to replace it")
    if current.exists() and not current.is_symlink():
        raise ValueError(f"{current} already exists and is not a release pointer")
    release = start_coordinator.release_target(source, install_dir)
    change_wrapper = not binary.exists() or binary.read_text() != expected
    if dry_run:
        return {"command": str(binary), "release": str(release),
                "wrapper_write": change_wrapper, "installed": False}
    if not bin_dir.is_dir():
        raise ValueError(f"binary directory {bin_dir} does not exist")
    release = start_coordinator.install(source, install_dir)
    if change_wrapper:
        with tempfile.NamedTemporaryFile("w", dir=bin_dir, prefix=".chief-of-stuff-", delete=False) as output:
            output.write(expected)
            staged = Path(output.name)
        try:
            staged.chmod(0o755)
            os.replace(staged, binary)
        finally:
            staged.unlink(missing_ok=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    staged_pointer = install_dir / f".current-{uuid.uuid4().hex}"
    try:
        staged_pointer.symlink_to(release.relative_to(install_dir))
        os.replace(staged_pointer, current)
    finally:
        staged_pointer.unlink(missing_ok=True)
    # §14: the same install brings the Claude Code plugin to this release, or reports where it stands.
    if str(SOURCE / "scripts") not in sys.path:
        sys.path.insert(0, str(SOURCE / "scripts"))
    import claude_adapter
    version = json.loads((release / ".claude-plugin/plugin.json").read_text())["version"]
    return {"command": str(binary), "release": str(release), "wrapper_write": change_wrapper, "installed": True,
            **claude_adapter.update_plugin(current, version)}


def install_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="chief-of-stuff install", description="Install the versioned release and one stable command")
    ap.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    ap.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    ap.add_argument("--replace-wrapper", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        result = install(source=SOURCE, install_dir=args.install_dir, bin_dir=args.bin_dir,
                         dry_run=args.dry_run, replace_wrapper=args.replace_wrapper)
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(toon_encode(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        names = "  ".join(("install", *COMMANDS))
        print("usage: chief-of-stuff <command> [arguments]\n\ncommands: " + names +
              "\n\nRun `chief-of-stuff <command> --help` for command options.")
        return 0
    if args[0] == "install":
        return install_main(args[1:])
    script = COMMANDS.get(args[0])
    if script is None:
        print(f"unknown chief-of-stuff command: {args[0]}", file=sys.stderr)
        return 2
    target = SOURCE / script
    if not target.is_file():
        print(f"installed command is missing: {target}", file=sys.stderr)
        return 1
    if args[0] in STORE_BACKED:
        scripts_dir = str(SOURCE / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import subprocess
        import shell_setup
        import supervisor
        try:
            data = supervisor.data_dir()
            with supervisor.lock(data / "supervisor.lock", wait=True):  # install holds it briefly; wait, do not refuse
                python = str(supervisor.ensure_venv(data))
        except (supervisor.ServiceError, shell_setup.ShellError, OSError, subprocess.SubprocessError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
    else:
        python = sys.executable
    os.execv(python, [python, str(target), *(["--board"] if args[0] == "board" else []), *args[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
