#!/usr/bin/env python3
"""One installed command for chief-of-stuff setup and operation.

`python3 chief_of_stuff.py install` copies a versioned release to the user's data directory
and writes a stable, tiny launcher to /usr/local/bin. Later installs only move the user-owned
`current` pointer when that launcher is already correct.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import start_coordinator
from scripts._vendor.toon_format import encode as toon_encode

SOURCE = Path(__file__).resolve().parent
DEFAULT_INSTALL_DIR = Path.home() / ".local" / "share" / "chief-of-stuff"
DEFAULT_BIN_DIR = Path("/usr/local/bin")
HELP_SECTIONS = ("Purpose", "When not to use", "Options", "Output", "Exit codes", "Side effects", "Examples")
COMMAND_GROUPS = ("engine", "intake", "forge", "observability")
# None hides a retiring command from discovery without removing its route. Retirement:
# #646 (install), #828 (start), #829 (log/tracker), #830 (worker/processes/result/adopt),
# #831 (inbox), #832 (audit/tick), #833 (notify), #834 (decision/sources),
# #835 (merge/review helpers), #836 (models). #835's kanban removal is conditional.
# A None script denotes a built-in command.
COMMAND_REGISTRY = {
    "install": (None, None),
    "start": ("start_coordinator.py", None),
    "init": ("scripts/init_workspace.py", "intake"),
    "work": ("scripts/work.py", "intake"),
    "engine": ("scripts/engine.py", "engine"),
    "supervisor": ("scripts/supervisor.py", "engine"),
    "models": ("scripts/install_model_guidance.py", None),
    "worktree": ("scripts/make_worktree.py", "engine"),
    "worker": ("scripts/spawn_session.py", None),
    "audit": ("scripts/audit_tasks.py", None),
    "processes": ("scripts/process_status.py", None),
    "result": ("scripts/one_shot_result.py", None),
    "adopt": ("scripts/adopt_one_shots.py", None),
    "board": ("scripts/pages.py", None),
    "inbox": ("scripts/inbox.py", None),
    "backlog": ("scripts/backlog.py", "intake"),
    "kanban": ("scripts/kanban_cli.py", "forge"),
    "pipeline": ("scripts/pipeline_cli.py", "forge"),
    "notify": ("scripts/notify_service.py", None),
    "health": ("scripts/probe_health.py", "observability"),
    "pages": ("scripts/pages.py", "observability"),
    "decision": ("scripts/decision_page.py", None),
    "decisions": ("scripts/decisions_cli.py", "observability"),
    "log": ("scripts/tracker_write.py", None),
    "tracker": ("scripts/tracker_read.py", None),
    "merge-approved": ("scripts/merge_approved.py", None),
    "merge-ready": ("scripts/merge_ready.py", None),
    "merge-order": ("scripts/merge_order.py", "forge"),
    "performance": ("scripts/performance_report.py", "observability"),
    "limits": ("scripts/limits_cli.py", "observability"),
    "review-threads": ("scripts/review_threads.py", None),
    "sources": ("scripts/board_sources.py", None),
    "tick": ("scripts/flow.py", None),
    "architect-signals": ("scripts/architect_signals_cli.py", "observability"),
    "docs": (None, "observability"),
}
# Preserve the public name-to-path lookup used by existing routing callers.
COMMANDS = {name: script for name, (script, _) in COMMAND_REGISTRY.items() if script is not None}
# Commands whose scripts import store (SQLAlchemy/Alembic).
STORE_BACKED = frozenset({"adopt", "board", "decision", "decisions", "engine", "limits", "notify", "pages", "performance", "tick", "work", "worker"})


def command_purpose(name: str) -> str:
    """Read a purpose without importing or executing the routed script."""
    script, _ = COMMAND_REGISTRY[name]
    if script is None:
        doc = {"install": install_main, "docs": docs_main}[name].__doc__
    else:
        doc = ast.get_docstring(ast.parse((SOURCE / script).read_text()))
    if not doc or not doc.strip():
        raise ValueError(f"command {name} has no purpose docstring")
    return doc.strip().splitlines()[0]


def top_level_help() -> str:
    lines = ["usage: chief-of-stuff <command> [arguments]", ""]
    for group in COMMAND_GROUPS:
        lines.append(f"{group}:")
        for name, (_, command_group) in COMMAND_REGISTRY.items():
            if command_group == group:
                lines.append(f"  {name}  {command_purpose(name)}")
        lines.append("")
    lines.extend(["Run `chief-of-stuff <command> --help` for command help.",
                  "Generated reference: docs/commands.md", ""])
    return "\n".join(lines)


def command_help(name: str) -> str:
    """Capture the command's own argparse help with stable formatting."""
    env = dict(os.environ, COLUMNS="100", NO_COLOR="1", PYTHON_COLORS="0", PYTHONHASHSEED="0")
    script = COMMAND_REGISTRY[name][0]
    # Store-backed scripts run under the current interpreter: generating docs must not build the engine venv.
    argv = [str(SOURCE / script)] if script else [str(SOURCE / "chief_of_stuff.py"), name]
    try:
        done = subprocess.run([sys.executable, *argv, "--help"],
                              env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"help failed for {name}: {exc}") from exc
    if done.returncode or not done.stdout.strip():
        raise ValueError(f"help failed for {name} (exit {done.returncode}): {done.stderr.strip()}")
    return done.stdout


def command_reference() -> str:
    """Generate the reference completely before emitting anything."""
    lines = ["# Command reference", "",
             "<!-- Generated from argparse help. Do not edit by hand.",
             "Regenerate (also to resolve conflicts):",
             "python3 chief_of_stuff.py docs > docs/commands.md", "-->", ""]
    for group in COMMAND_GROUPS:
        lines.extend([f"## {group.capitalize()}", ""])
        for name, (_, command_group) in COMMAND_REGISTRY.items():
            if command_group == group:
                lines.extend([f"### `{name}`", "", "```text", command_help(name).rstrip(), "```", ""])
    return "\n".join(lines)


def docs_main(argv: list[str]) -> int:
    """Generate the command reference from each command's own argparse help."""
    ap = argparse.ArgumentParser(
        prog="chief-of-stuff docs", description=docs_main.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""When not to use:
  For one command's options, run chief-of-stuff <command> --help.

Output:
  The complete Markdown command reference on stdout; errors on stderr.

Exit codes:
  0: reference generated; 1: a command's help failed; 2: invalid arguments.

Side effects:
  Runs each visible command with --help. Writes no files itself.

Examples:
  chief-of-stuff docs
  chief-of-stuff docs > docs/commands.md"""
    )
    ap.parse_args(argv)
    try:
        reference = command_reference()
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(reference, end="")
    return 0


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
    return {"command": str(binary), "release": str(release),
            "wrapper_write": change_wrapper, "installed": True}


def install_main(argv: list[str]) -> int:
    """Install the versioned release and one stable command."""
    ap = argparse.ArgumentParser(
        prog="chief-of-stuff install", description=install_main.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""When not to use:
  To operate an existing installation, use its commands directly.

Output:
  A TOON record of the command path, release path, wrapper write and install status.
  Refusals are printed on stderr.

Exit codes:
  0: installed or previewed; 1: installation refused; 2: invalid arguments.

Side effects:
  Copies a versioned release, updates the current symlink and may write the launcher.
  With --dry-run, writes nothing.

Examples:
  chief-of-stuff install --dry-run
  chief-of-stuff install --bin-dir /usr/local/bin"""
    )
    ap.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR,
                    help=f"release directory (default: {DEFAULT_INSTALL_DIR})")
    ap.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR,
                    help=f"launcher directory (default: {DEFAULT_BIN_DIR})")
    ap.add_argument("--replace-wrapper", action="store_true", help="replace a differing launcher after inspection")
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
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
        print(top_level_help(), end="")
        return 0
    if args[0] == "install":
        return install_main(args[1:])
    if args[0] == "docs":
        return docs_main(args[1:])
    script = COMMANDS.get(args[0])
    if script is None:
        print(f"unknown chief-of-stuff command: {args[0]}", file=sys.stderr)
        return 2
    target = SOURCE / script
    if not target.is_file():
        print(f"installed command is missing: {target}", file=sys.stderr)
        return 1
    if args[0] in STORE_BACKED and {"-h", "--help"} & set(args[1:]):
        # Help must not install the engine venv (offline CI, first run). Bootstrap only when this interpreter
        # cannot import the script's dependencies.
        done = subprocess.run([sys.executable, str(target), *(["--board"] if args[0] == "board" else []), *args[1:]],
                              capture_output=True, text=True)
        if "ModuleNotFoundError" not in done.stderr:
            sys.stdout.write(done.stdout)
            sys.stderr.write(done.stderr)
            return done.returncode
    if args[0] in STORE_BACKED:
        scripts_dir = str(SOURCE / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
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
