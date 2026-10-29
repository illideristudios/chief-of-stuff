"""The engine's launchd LaunchAgent and the venv it runs in, plus the LaunchAgent lifecycle notify_service shares."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import shell_setup

RELEASE = Path(__file__).resolve().parent.parent

ACTIONS = ("install", "uninstall", "status", "ensure", "ensure-background")
DISABLED_MARKER = "engine.disabled"
PLUGIN_RELEASE_MARKER = "engine-plugin-release"
ENSURE_LOCK_TIMEOUT = 1200.0

# Run after the venv python, with the workspace as working directory (engine --root defaults to ".").
ENGINE: tuple[str, ...] = (str(RELEASE / "scripts/engine.py"),)


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
def lock(path: Path, wait: bool = False, timeout: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait and deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if deadline is None:
                    raise ServiceError(f"already running or being managed ({path.name} is held)") from None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ServiceError(f"timed out waiting for {path.name}") from None
                time.sleep(min(0.25, remaining))
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


def version(release: Path | None = None) -> str:
    return json.loads(((release or RELEASE) / ".claude-plugin/plugin.json").read_text())["version"]


def current_release() -> Path:
    """Follow `current` only when it is a symlink beside `versions/`; otherwise return RELEASE."""
    pointer = RELEASE.parent.parent / "current"
    try:
        if RELEASE.parent.name == "versions" and pointer.is_symlink():
            selected = pointer.resolve(strict=True)
            if not (selected / "chief_of_stuff.py").is_file():
                raise ServiceError(f"missing entry file: {selected / 'chief_of_stuff.py'}")
            return selected
    except (OSError, RuntimeError, ServiceError) as exc:
        print(f"engine: cannot follow current release {pointer}: {exc}; keeping {RELEASE}", file=sys.stderr, flush=True)
    return RELEASE


def _plugin_version(release: Path) -> tuple[int, ...] | None:
    """Recognize versioned Claude cache entries, never an arbitrary checkout or installed release."""
    parent = release.parent
    if len(parent.parents) < 3 or parent.parent.parent.name != "cache" or parent.parent.parent.parent.name != "plugins":
        return None
    try:
        value = version(release)
        if not value or any(not part.isdigit() for part in value.split(".")):
            return None
        if not (release / "scripts/engine.py").is_file() or not (release / "requirements.txt").is_file():
            return None
        return tuple(int(part) for part in value.split("."))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def selected_release(root: Path) -> Path:
    """Follow the installed pointer, or a validated newer sibling requested by a plugin session."""
    current = current_release()
    current_version = _plugin_version(current)
    if current != RELEASE or current_version is None:
        return current
    try:
        candidate = Path((state_dir(root) / PLUGIN_RELEASE_MARKER).read_text()).resolve(strict=True)
        candidate_version = _plugin_version(candidate)
        if (candidate.parent == current.parent and candidate_version is not None
                and candidate_version > current_version):
            return candidate
    except (OSError, RuntimeError, ValueError):
        pass
    return current


def _request_plugin_release(root: Path, selected: Path, existing: dict) -> None:
    """Publish a newer cache sibling under the global lock after its dependencies are prepared."""
    try:
        running = Path(existing["ProgramArguments"][1]).resolve().parent.parent
        candidate_version = _plugin_version(selected)
        running_version = _plugin_version(running)
        prior_version = None
        marker = state_dir(root) / PLUGIN_RELEASE_MARKER
        try:
            prior = Path(marker.read_text()).resolve(strict=True)
            if prior.parent == running.parent:
                prior_version = _plugin_version(prior)
        except (OSError, RuntimeError, ValueError):
            pass
        baseline = max((v for v in (running_version, prior_version) if v is not None), default=None)
        if (selected.parent == running.parent and candidate_version is not None and baseline is not None
                and candidate_version > baseline):
            atomic_write(marker, str(selected).encode())
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        return


def _venv_python_runs(python: Path, *, timeout: float = 30) -> bool:
    try:
        return python.is_file() and subprocess.run(
            [str(python), "-c", ""], capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_preparation(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Reap a timed-out venv or pip process and its descendants before discarding its generation."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as child:
        try:
            out, err = child.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise
        return subprocess.CompletedProcess(argv, child.returncode, out, err)


def ensure_venv(data: Path, *, release: Path | None = None, timeout: float = 900) -> Path:
    """Return the selected release's venv, publishing a fresh generation only after installation succeeds.

    Callers hold supervisor.lock. The total preparation budget includes shell lookup and all subprocesses.
    Published generations stay at their original paths: other workspaces and launchd may still use them.
    The returned interpreter is reached through the `venv-<key>.current` symlink, so a LaunchAgent that
    recorded it follows every later repair of the same pins and base Python without being reinstalled.
    A running engine follows that link through sys.prefix, including a repair another workspace published.
    """
    deadline = time.monotonic() + timeout

    def remaining(cap: float = 900) -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired("engine dependency preparation", timeout)
        return min(seconds, cap)

    requirements = (release or RELEASE) / "requirements.txt"
    base = shell_setup.resolve("python3")
    if base is None:
        raise ServiceError("no python3 on the login shell's PATH to build the engine venv")
    try:
        identity = subprocess.run(
            [base, "-c", "import json, sys; print(json.dumps([sys.version, sys.implementation.cache_tag]))"],
            capture_output=True, text=True, timeout=remaining(30))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"could not identify the engine's base Python: {exc}") from exc
    if identity.returncode:
        raise ServiceError(f"could not identify the engine's base Python: {(identity.stderr or identity.stdout).strip()[-2000:]}")
    # Changed pins or Python identity use a separate venv; repairs also get a new generation.
    # shortcut: superseded venvs are never removed, prune them once an engine per workspace is common.
    key = hashlib.sha256(requirements.read_bytes() + b"\0" + identity.stdout.encode()).hexdigest()[:16]
    legacy, pointer = f"venv-{key}", data / f"venv-{key}.current"
    try:
        name = os.readlink(pointer)
    except FileNotFoundError:
        name = legacy  # reuse environments published before generation pointers
    except OSError:
        name = ""  # not a symlink: a cache miss, replaced by the build below
    if name.startswith(legacy) and Path(name).name == name:
        target = data / name
        launch = pointer if pointer.is_symlink() else target
        if (target / ".installed").exists() and _venv_python_runs(launch / "bin/python", timeout=remaining(30)):
            if not pointer.is_symlink():
                # A pre-pointer directory has no link; publish one so a recorded LaunchAgent follows later repairs.
                staged = data / f".{pointer.name}.{time.monotonic_ns()}"
                try:
                    os.symlink(name, staged)
                    os.replace(staged, pointer)
                except BaseException:
                    staged.unlink(missing_ok=True)
                    raise
            return pointer / "bin/python"
    data.mkdir(parents=True, exist_ok=True)
    building = Path(tempfile.mkdtemp(dir=data, prefix=f"{legacy}-"))
    python = building / "bin/python"
    staged = data / f".{pointer.name}.{building.name}"
    try:
        for argv in ([base, "-m", "venv", str(building)],
                     [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)]):
            result = run_preparation(argv, timeout=remaining())
            if result.returncode:
                raise ServiceError(f"engine dependencies failed to install: "
                                   f"{(result.stderr or result.stdout).strip()[-2000:]}")
        (building / ".installed").touch()
        os.symlink(building.name, staged)
        os.replace(staged, pointer)
    except BaseException:
        staged.unlink(missing_ok=True)
        # A published generation stays: even cancellation after the replace leaves it available to other callers.
        if not (pointer.is_symlink() and os.readlink(pointer) == building.name):
            shutil.rmtree(building, ignore_errors=True)
        raise
    return pointer / "bin/python"


def require_macos():
    if sys.platform != "darwin":
        raise ServiceError("the engine supervisor requires macOS (launchd)")


def launch_env(release: Path | None = None) -> dict:
    """Pass the selected release version and configured paths to launchd."""
    env = {"CLAUDE_PLUGIN_DATA": str(data_dir()), "CHIEF_OF_STUFF_VERSION": version(release)}
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_AS_PROFILES_DIR", "XDG_STATE_HOME"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def install(root: Path, engine: tuple[str, ...] | None = None, jobs_dir: Path | None = None,
            *, respect_disabled: bool = False) -> str:
    """Start the engine under launchd, or restart it when the plugin version, its path or its pins changed."""
    selected = current_release() if engine is None else RELEASE
    if engine is None:
        engine = (str(selected / "scripts/engine.py"),)
    if not engine:
        raise ServiceError("the engine entry command is empty")
    require_macos()
    root, data = root.resolve(), data_dir()
    with lock(data / "supervisor.lock", timeout=ENSURE_LOCK_TIMEOUT if respect_disabled else None):
        if respect_disabled and (state_dir(root) / DISABLED_MARKER).is_file():
            return "engine: disabled"
        if respect_disabled:
            from workspace import lock_held
            path = job_path(label("engine", root), jobs_dir)
            if path.exists() and lock_held(state_dir(root) / "engine.lock"):
                existing = plistlib.loads(path.read_bytes())
                if existing.get("Label") != label("engine", root) or existing.get("WorkingDirectory") != str(root):
                    raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
                if launchctl("print", f"gui/{os.getuid()}/{label('engine', root)}", missing_ok=True):
                    # Prepare the selected release without disrupting the pass; the engine follows it at a boundary.
                    ensure_venv(data, release=selected)
                    _request_plugin_release(root, selected, existing)
                    return "engine: serving"
        log_path = state_dir(root) / "engine.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = str(log_path)
        desired = {"Label": label("engine", root),
                   # Not resolved: the venv python is a symlink, and resolving it would leave the venv.
                   "ProgramArguments": [str(ensure_venv(data, release=selected)), *engine],
                   "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                   "EnvironmentVariables": launch_env(selected),
                   "StandardOutPath": log, "StandardErrorPath": log}
        outcome = load_agent(desired, jobs_dir)
        (state_dir(root) / DISABLED_MARKER).unlink(missing_ok=True)
        return "engine: " + outcome


def ensure(root: Path, jobs_dir: Path | None = None) -> str:
    """Ensure the engine LaunchAgent is installed for store-backed workspaces unless explicitly uninstalled."""
    resolved = root.resolve()
    if not (resolved / "store.db").is_file():
        return "engine: not configured"
    if (state_dir(resolved) / DISABLED_MARKER).is_file():
        return "engine: disabled"
    return install(resolved, jobs_dir=jobs_dir, respect_disabled=True)


def ensure_background(root: Path) -> str:
    """Start bounded ensure outside the Claude hook lifetime; diagnostics go to the workspace log."""
    root = root.resolve()
    if not (root / "store.db").is_file():
        return "engine: not configured"
    log = state_dir(root) / "engine-ensure.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as output:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "ensure"],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    return "engine: ensuring"


def uninstall(root: Path, jobs_dir: Path | None = None) -> str:
    require_macos()
    resolved = root.resolve()
    with lock(data_dir() / "supervisor.lock"):
        unload_agent(label("engine", resolved), resolved, jobs_dir)
        atomic_write(state_dir(resolved) / DISABLED_MARKER, b"")
    return "engine: stopped"


def status(root: Path, jobs_dir: Path | None = None) -> dict:
    name = label("engine", root)
    path = job_path(name, jobs_dir)
    job = plistlib.loads(path.read_bytes()) if path.exists() else None
    return {"installed": job is not None,
            "loaded": sys.platform == "darwin" and launchctl("print", f"gui/{os.getuid()}/{name}", missing_ok=True),
            "version": job and job.get("EnvironmentVariables", {}).get("CHIEF_OF_STUFF_VERSION"),
            "current": version(current_release())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="workspace root the engine serves")
    ap.add_argument("cmd", choices=ACTIONS)
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.cmd == "status":
            from _vendor.toon_format import encode
            print(encode(status(root)))
        elif args.cmd == "ensure":
            print(ensure(root))
        elif args.cmd == "ensure-background":
            print(ensure_background(root))
        else:
            print(install(root) if args.cmd == "install" else uninstall(root))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Unwind preparation so its subprocess group is reaped on service termination.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    sys.exit(main())
