"""The engine's launchd LaunchAgent and the venv it runs in, plus the LaunchAgent lifecycle notify_service shares."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import shell_setup

RELEASE = Path(__file__).resolve().parent.parent

# M3 engine loop: fill in the engine's entry argv, run after the venv python with the workspace as working directory,
# e.g. (str(RELEASE / "scripts/engine.py"), "serve"). Until then `install` refuses.
ENGINE: tuple[str, ...] = ()


class ServiceError(RuntimeError):
    pass


def atomic_write(path: Path, data: bytes, mode: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:  # mkstemp makes 0600; a served page is world-readable
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def state_dir(root: Path) -> Path:
    return root.resolve() / ".chief-of-stuff"


@contextmanager
def lock(path: Path, wait: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ServiceError(f"already running or being managed ({path.name} is held)") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def label(kind: str, root: Path) -> str:
    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:20]
    return f"com.chief-of-stuff.{kind}.{key}"


def launchctl(*args: str, missing_ok: bool = False) -> bool:
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        if missing_ok and any(word in result.stderr.lower() for word in
                              ("could not find service", "no such process", "service not found")):
            return False
        raise ServiceError(result.stderr.strip() or f"launchctl exited {result.returncode}")
    return True


def job_path(name: str, jobs_dir: Path | None) -> Path:
    return (jobs_dir or Path.home() / "Library/LaunchAgents") / f"{name}.plist"


def load_agent(desired: dict, jobs_dir: Path | None) -> str:
    """Install or replace the user LaunchAgent `desired` describes; a changed job is booted out and started again."""
    name = desired["Label"]
    target = f"gui/{os.getuid()}/{name}"
    path = job_path(name, jobs_dir)
    existing = plistlib.loads(path.read_bytes()) if path.exists() else None
    if existing is not None and (existing.get("Label") != name or
                                 existing.get("WorkingDirectory") != desired["WorkingDirectory"]):
        raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
    loaded = launchctl("print", target, missing_ok=True)
    if existing == desired and loaded:
        return "serving"
    if loaded:
        launchctl("bootout", target)
        time.sleep(0.05)
    atomic_write(path, plistlib.dumps(desired))
    # unload removes the plist; enable also recovers a job disabled externally.
    launchctl("enable", target)
    for attempt in range(5):
        try:
            launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
            break
        except ServiceError as exc:
            if "Input/output error" in str(exc) and attempt < 4:
                time.sleep(0.1 * (attempt + 1))
                launchctl("bootout", target, missing_ok=True)
                continue
            raise
    return "restarted" if existing or loaded else "started"


def unload_agent(name: str, workdir: Path, jobs_dir: Path | None):
    path = job_path(name, jobs_dir)
    target = f"gui/{os.getuid()}/{name}"
    if path.exists():
        job = plistlib.loads(path.read_bytes())
        if job.get("Label") != name or job.get("WorkingDirectory") != str(workdir):
            raise ServiceError(f"refusing to remove unrelated LaunchAgent: {path}")
    # Remove the login registration first so an interrupted stop cannot leave
    # a job which reappears at the next login.
    path.unlink(missing_ok=True)
    if launchctl("print", target, missing_ok=True):
        launchctl("bootout", target)
        time.sleep(0.05)


def data_dir() -> Path:
    # Unset outside a Claude Code hook; the fallback is the dir Claude Code gives this plugin from its own marketplace.
    return Path(os.environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".claude/plugins/data/chief-of-stuff-chief-of-stuff")


def version() -> str:
    return json.loads((RELEASE / ".claude-plugin/plugin.json").read_text())["version"]


def _venv_python_runs(python: Path) -> bool:
    try:
        return python.is_file() and subprocess.run(
            [str(python), "-c", ""], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_venv(data: Path) -> Path:
    """The venv python for the pinned requirements.txt and the login shell's base Python identity."""
    requirements = RELEASE / "requirements.txt"
    base = shell_setup.resolve("python3")
    if base is None:
        raise ServiceError("no python3 on the login shell's PATH to build the engine venv")
    try:
        identity = subprocess.run(
            [base, "-c", "import json, sys; print(json.dumps([sys.version, sys.implementation.cache_tag]))"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"could not identify the engine's base Python: {exc}") from exc
    if identity.returncode:
        raise ServiceError(f"could not identify the engine's base Python: {(identity.stderr or identity.stdout).strip()[-2000:]}")
    # Keyed by the pins and Python identity, so a rebuild never touches the venv a running engine uses.
    # shortcut: superseded venvs are never removed, prune them once an engine per workspace is common.
    key = hashlib.sha256(requirements.read_bytes() + b"\0" + identity.stdout.encode()).hexdigest()[:16]
    venv = data / f"venv-{key}"
    python, done = venv / "bin/python", venv / ".installed"
    if done.exists() and _venv_python_runs(python):
        return python
    shutil.rmtree(venv, ignore_errors=True)
    for argv in ([base, "-m", "venv", str(venv)],
                 [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)]):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=900)
        if result.returncode:
            raise ServiceError(f"engine dependencies failed to install, engine not started: "
                               f"{(result.stderr or result.stdout).strip()[-2000:]}")
    done.touch()
    return python


def require_macos():
    if sys.platform != "darwin":
        raise ServiceError("the engine supervisor requires macOS (launchd)")


def install(root: Path, engine: tuple[str, ...] = ENGINE, jobs_dir: Path | None = None) -> str:
    """Start the engine under launchd, or restart it when the plugin version, its path or its pins changed."""
    if not engine:
        raise ServiceError("the engine entry command is not set yet (supervisor.ENGINE, M3 engine loop)")
    require_macos()
    root, data = root.resolve(), data_dir()
    with lock(data / "supervisor.lock"):
        log_path = state_dir(root) / "engine.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = str(log_path)
        desired = {"Label": label("engine", root),
                   # Not resolved: the venv python is a symlink, and resolving it would leave the venv.
                   "ProgramArguments": [str(ensure_venv(data)), *engine],
                   "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                   # The version makes an updated plugin a changed job, which load_agent restarts.
                   "EnvironmentVariables": {"CLAUDE_PLUGIN_DATA": str(data), "CHIEF_OF_STUFF_VERSION": version()},
                   "StandardOutPath": log, "StandardErrorPath": log}
        return "engine: " + load_agent(desired, jobs_dir)


def uninstall(root: Path, jobs_dir: Path | None = None) -> str:
    require_macos()
    with lock(data_dir() / "supervisor.lock"):
        unload_agent(label("engine", root), root.resolve(), jobs_dir)
    return "engine: stopped"


def status(root: Path, jobs_dir: Path | None = None) -> dict:
    name = label("engine", root)
    path = job_path(name, jobs_dir)
    job = plistlib.loads(path.read_bytes()) if path.exists() else None
    return {"installed": job is not None,
            "loaded": sys.platform == "darwin" and launchctl("print", f"gui/{os.getuid()}/{name}", missing_ok=True),
            "version": job and job.get("EnvironmentVariables", {}).get("CHIEF_OF_STUFF_VERSION"),
            "current": version()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="workspace root the engine serves")
    ap.add_argument("cmd", choices=("install", "uninstall", "status"))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.cmd == "status":
            from _vendor.toon_format import encode
            print(encode(status(root)))
        else:
            print(install(root) if args.cmd == "install" else uninstall(root))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
