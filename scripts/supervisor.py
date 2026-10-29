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
import engine_intent

RELEASE = Path(__file__).resolve().parent.parent

ACTIONS = ("install", "uninstall", "status", "ensure", "ensure-background", "ensure-retry", "commit-handoff")
DISABLED_MARKER = "engine.disabled"
PLUGIN_RELEASE_MARKER = "engine-plugin-release"
HANDOFF_MARKER = "engine-handoff.json"
EXPLICIT_INSTALL_MARKER = "engine-explicit-install.json"
EXPLICIT_INTENT_MARKER = "engine-explicit-intent.json"
TRUSTED_INTENT_VERSION = 2
ENSURE_LOCK_TIMEOUT = 1200.0

# Run after the venv python, with the workspace as working directory (engine --root defaults to ".").
ENGINE: tuple[str, ...] = (str(RELEASE / "scripts/engine.py"),)


class ServiceError(RuntimeError):
    pass


class LockTimeout(ServiceError):
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
                    raise LockTimeout(f"timed out waiting for {path.name}") from None
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


def load_agent(desired: dict, jobs_dir: Path | None, *, defer_loaded: bool = False) -> str:
    """Install or replace the user LaunchAgent `desired` describes; a changed job is booted out and started again."""
    name = desired["Label"]
    target = f"gui/{os.getuid()}/{name}"
    path = job_path(name, jobs_dir)
    existing = plistlib.loads(path.read_bytes()) if path.exists() else None
    if existing is not None and (existing.get("Label") != name or
                                 existing.get("WorkingDirectory") != desired["WorkingDirectory"]):
        raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
    loaded = launchctl("print", target, missing_ok=True)
    if loaded and defer_loaded:
        return "upgrade pending"
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
    intent = _explicit_intent(root)
    if intent and intent["operation"] == "install":
        chosen = Path(intent["release"])
        current = current_release()
        # Installed versions follow the user's current pointer; cache choices stay pinned.
        if not intent["pending"] and chosen.parent.name == "versions" and current.parent == chosen.parent:
            _validate_release(root, current)
            return current
        _validate_release(root, chosen)
        return chosen
    pending = _explicit_install(root)
    if pending is not None:
        chosen = Path(pending["release"])
        _validate_release(root, chosen)
        return chosen
    current = current_release()
    _validate_release(root, current)
    current_version = _plugin_version(current)
    if current != RELEASE or current_version is None:
        return current
    try:
        candidate = Path((state_dir(root) / PLUGIN_RELEASE_MARKER).read_text()).resolve(strict=True)
        _validate_release(root, candidate)
        candidate_version = _plugin_version(candidate)
        if (candidate.parent == current.parent and candidate_version is not None
                and candidate_version > current_version):
            return candidate
    except (OSError, RuntimeError, ValueError, ServiceError):
        pass
    return current


def _write_intent(root: Path, intent: dict) -> None:
    path = engine_intent.intent_path(root, data_dir(), ServiceError, create=True)
    atomic_write(path, json.dumps(intent).encode())


def _explicit_intent(root: Path) -> dict | None:
    return engine_intent.read(root, data_dir(), ServiceError)


def _validate_release(root: Path, release: Path) -> None:
    engine_intent.validate_release(root, release, ServiceError)


def _recover_uninstall(root: Path, jobs_dir: Path | None) -> None:
    intent = _explicit_intent(root)
    if intent and intent["operation"] == "uninstall" and intent.get("pending"):
        unload_agent(label("engine", root), root, jobs_dir)
        atomic_write(state_dir(root) / DISABLED_MARKER, b"")
        (state_dir(root) / HANDOFF_MARKER).unlink(missing_ok=True)
        (state_dir(root) / EXPLICIT_INSTALL_MARKER).unlink(missing_ok=True)
        _write_intent(root, {"operation": "uninstall", "pending": False})


def _disabled(root: Path) -> bool:
    intent = _explicit_intent(root)
    if intent is not None:
        return intent["operation"] == "uninstall"
    return (state_dir(root) / DISABLED_MARKER).is_file()


def _explicit_install(root: Path) -> dict | None:
    """Read an incomplete explicit install; malformed state must not select another release."""
    intent = _explicit_intent(root)
    if intent is not None:
        return intent if intent["operation"] == "install" and intent["pending"] else None
    # Legacy workspace records are untrusted, including records left by older releases.
    return None


def _finish_explicit_install(root: Path, pending: dict, jobs_dir: Path | None, *, replay: bool,
                             defer_loaded: bool = False) -> str:
    """Replay uncertain launchd state before retiring the explicit target."""
    job = pending["job"]
    if replay:
        path = job_path(job["Label"], jobs_dir)
        if path.exists():
            existing = plistlib.loads(path.read_bytes())
            if existing.get("Label") != job["Label"] or existing.get("WorkingDirectory") != str(root):
                raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
        target = f"gui/{os.getuid()}/{job['Label']}"
        if launchctl("print", target, missing_ok=True):
            if defer_loaded:
                return _upgrade_pending()
            launchctl("bootout", target)
    outcome = load_agent(job, jobs_dir, defer_loaded=True) if defer_loaded else load_agent(job, jobs_dir)
    if outcome == "upgrade pending":
        return _upgrade_pending()
    (state_dir(root) / PLUGIN_RELEASE_MARKER).unlink(missing_ok=True)
    (state_dir(root) / HANDOFF_MARKER).unlink(missing_ok=True)
    (state_dir(root) / DISABLED_MARKER).unlink(missing_ok=True)
    intent = _explicit_intent(root)
    if intent is not None and intent["operation"] == "install":
        _write_intent(root, {**intent, "pending": False})
    else:
        # Preserve an explicitly supplied trusted operation if the record was removed.
        _write_intent(root, {**pending, "operation": "install", "pending": False})
    (state_dir(root) / EXPLICIT_INSTALL_MARKER).unlink(missing_ok=True)
    return "engine: " + outcome


def _request_plugin_release(root: Path, selected: Path, existing: dict) -> bool:
    """Publish a newer cache sibling under the global lock after its dependencies are prepared."""
    try:
        running = Path(existing["ProgramArguments"][1]).resolve().parent.parent
        if selected == running:
            return True
        candidate_version = _plugin_version(selected)
        running_version = _plugin_version(running)
        prior_version = None
        marker = state_dir(root) / PLUGIN_RELEASE_MARKER
        try:
            prior = Path(marker.read_text()).resolve(strict=True)
            if prior.parent == running.parent:
                _validate_release(root, prior)
                prior_version = _plugin_version(prior)
        except (OSError, RuntimeError, ValueError, ServiceError):
            pass
        baseline = max((v for v in (running_version, prior_version) if v is not None), default=None)
        if (selected.parent == running.parent and candidate_version is not None and baseline is not None
                and candidate_version > baseline):
            atomic_write(marker, str(selected).encode())
            return True
        # A previously requested newer sibling already satisfies an older session's ensure.
        return (selected.parent == running.parent and candidate_version is not None and baseline is not None
                and candidate_version <= baseline)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        return False


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


def _upgrade_pending() -> str:
    return ("engine: upgrade pending; if the running engine cannot hand off, stop new work, "
            "verify the current pass has completed and no new pass can start, then run "
            "chief-of-stuff supervisor --root /path/to/workspace install from the desired release during a maintenance window")


def install(root: Path, engine: tuple[str, ...] | None = None, jobs_dir: Path | None = None,
            *, respect_disabled: bool = False) -> str:
    """Start the engine under launchd, or restart it when the plugin version, its path or its pins changed."""
    automatic_engine = engine is None
    selected = current_release() if engine is None else RELEASE
    if engine is None:
        engine = (str(selected / "scripts/engine.py"),)
    if not engine:
        raise ServiceError("the engine entry command is empty")
    require_macos()
    root, data = root.resolve(), data_dir()
    _validate_release(root, selected)
    with lock(data / "supervisor.lock", timeout=ENSURE_LOCK_TIMEOUT if respect_disabled else None):
        if respect_disabled and _disabled(root):
            _recover_uninstall(root, jobs_dir)
            return "engine: disabled"
        if respect_disabled:
            pending = _explicit_install(root)
            if pending is not None:
                path = job_path(label("engine", root), jobs_dir)
                if path.exists():
                    existing = plistlib.loads(path.read_bytes())
                    if existing.get("Label") != label("engine", root) or existing.get("WorkingDirectory") != str(root):
                        raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
                target = f"gui/{os.getuid()}/{label('engine', root)}"
                # The workspace can unlink a held lock. A loaded job may still be
                # in a pass, so only the engine's boundary helper can replace it.
                if launchctl("print", target, missing_ok=True):
                    return _upgrade_pending()
                return _finish_explicit_install(root, pending, jobs_dir, replay=True, defer_loaded=True)
            path = job_path(label("engine", root), jobs_dir)
            if automatic_engine:
                selected = selected_release(root)
                if path.exists():
                    existing = plistlib.loads(path.read_bytes())
                    if existing.get("Label") != label("engine", root) or existing.get("WorkingDirectory") != str(root):
                        raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
                    registered = current_release_from_job(existing)
                    selected_version, registered_version = _plugin_version(selected), _plugin_version(registered)
                    if (_explicit_intent(root) is None and registered.parent == selected.parent and selected_version is not None
                            and registered_version is not None and registered_version > selected_version):
                        selected = registered
                _validate_release(root, selected)
                engine = (str(selected / "scripts/engine.py"),)
            target = f"gui/{os.getuid()}/{label('engine', root)}"
            loaded = launchctl("print", target, missing_ok=True)
            if loaded and not path.exists():
                return _upgrade_pending()
            if loaded:
                existing = plistlib.loads(path.read_bytes())
                if existing.get("Label") != label("engine", root) or existing.get("WorkingDirectory") != str(root):
                    raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
                # A workspace lock can be unlinked while held; a loaded job is never proof of idleness.
                ensure_venv(data, release=selected)
                running = current_release_from_job(existing)
                installed_pointer = (selected.parent.name == "versions" and running.parent == selected.parent
                                     and (selected.parent.parent / "current").is_symlink())
                if not _request_plugin_release(root, selected, existing) and not installed_pointer:
                    return _upgrade_pending()
                return _upgrade_pending() if running != selected else "engine: serving"
        log_path = state_dir(root) / "engine.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = str(log_path)
        desired = {"Label": label("engine", root),
                   # Not resolved: the venv python is a symlink, and resolving it would leave the venv.
                   "ProgramArguments": [str(ensure_venv(data, release=selected)), *engine],
                   "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                   "EnvironmentVariables": launch_env(selected),
                   "StandardOutPath": log, "StandardErrorPath": log}
        if respect_disabled and automatic_engine and path.exists():
            existing = plistlib.loads(path.read_bytes())
            _request_plugin_release(root, selected, existing)
        if not respect_disabled:
            pending = {"release": str(selected), "job": desired}
            _write_intent(root, {**pending, "operation": "install", "pending": True})
            atomic_write(state_dir(root) / EXPLICIT_INSTALL_MARKER, b"{}")
            return _finish_explicit_install(root, pending, jobs_dir, replay=False)
        outcome = load_agent(desired, jobs_dir, defer_loaded=True)
        if outcome == "upgrade pending":
            return _upgrade_pending()
        (state_dir(root) / DISABLED_MARKER).unlink(missing_ok=True)
        return "engine: " + outcome


def ensure(root: Path, jobs_dir: Path | None = None) -> str:
    """Ensure the engine LaunchAgent is installed for store-backed workspaces unless explicitly uninstalled."""
    resolved = root.resolve()
    if not (resolved / "store.db").is_file():
        return "engine: not configured"
    return install(resolved, jobs_dir=jobs_dir, respect_disabled=True)


def ensure_background(root: Path) -> str:
    """Start bounded ensure outside the Claude hook lifetime; diagnostics go to the workspace log."""
    root = root.resolve()
    if not (root / "store.db").is_file():
        return "engine: not configured"
    log = state_dir(root) / "engine-ensure.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as output:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "ensure-retry"],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    return "engine: ensuring"


def current_release_from_job(job: dict) -> Path:
    try:
        return Path(job["ProgramArguments"][1]).resolve().parent.parent
    except (KeyError, IndexError, TypeError, OSError, RuntimeError):
        raise ServiceError("cannot identify existing engine release") from None


def ensure_retry(root: Path) -> str:
    """One detached worker per workspace retries timed-out lock acquisitions until resolved."""
    try:
        with lock(state_dir(root) / "engine-ensure.lock", wait=True):
            while True:
                try:
                    return ensure(root)
                except LockTimeout as exc:
                    print(f"engine: {exc}; retrying", file=sys.stderr, flush=True)
                    time.sleep(5)
    except ServiceError as exc:
        if "engine-ensure.lock is held" in str(exc):
            return "engine: ensure already pending"
        raise


def commit_handoff(root: Path, selected: Path, jobs_dir: Path | None = None) -> str:
    """Replace the launchd registration at a pass boundary, without racing a newer install."""
    root = root.resolve()
    with lock(data_dir() / "supervisor.lock", timeout=ENSURE_LOCK_TIMEOUT):
        path = job_path(label("engine", root), jobs_dir)
        if _disabled(root):
            _recover_uninstall(root, jobs_dir)
            return "engine: handoff superseded"
        if selected_release(root) != selected:
            return "engine: handoff superseded"
        pending_install = _explicit_install(root)
        if pending_install is not None:
            return _finish_explicit_install(root, pending_install, jobs_dir, replay=True)
        if not path.exists():
            return "engine: handoff superseded"
        job = plistlib.loads(path.read_bytes())
        if job.get("Label") != label("engine", root) or job.get("WorkingDirectory") != str(root):
            raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
        marker = state_dir(root) / HANDOFF_MARKER
        try:
            pending = json.loads(marker.read_text())
        except FileNotFoundError:
            pending = None
        if pending is None and selected == RELEASE:
            return "engine: handoff superseded"
        job_release = current_release_from_job(job)
        if pending is not None and str(job_release) not in (pending.get("from"), pending.get("to")):
            raise ServiceError("handoff record does not match the engine registration")
        if job_release != RELEASE and pending is None:
            return "engine: handoff superseded"
        _validate_release(root, selected)
        python = ensure_venv(data_dir(), release=selected)
        old_arguments, old_environment = job["ProgramArguments"], job.get("EnvironmentVariables", {})
        job["ProgramArguments"] = [str(python), str(selected / "scripts/engine.py")]
        job["EnvironmentVariables"] = launch_env(selected)
        if pending is None or pending.get("to") != str(selected):
            atomic_write(marker, json.dumps({"from": str(job_release), "to": str(selected)}).encode())
        atomic_write(path, plistlib.dumps(job))
        target = f"gui/{os.getuid()}/{label('engine', root)}"
        if launchctl("print", target, missing_ok=True):
            try:
                launchctl("bootout", target)
            except ServiceError:
                # The old job is still loaded; retain its matching registration on failure.
                job["ProgramArguments"] = old_arguments
                job["EnvironmentVariables"] = old_environment
                atomic_write(path, plistlib.dumps(job))
                if pending is None:
                    marker.unlink(missing_ok=True)
                raise
        outcome = load_agent(job, jobs_dir)
        marker.unlink(missing_ok=True)
        return "engine: " + outcome


def uninstall(root: Path, jobs_dir: Path | None = None) -> str:
    require_macos()
    resolved = root.resolve()
    with lock(data_dir() / "supervisor.lock"):
        _write_intent(resolved, {"operation": "uninstall", "pending": True})
        _recover_uninstall(resolved, jobs_dir)
    return "engine: stopped"


def status(root: Path, jobs_dir: Path | None = None) -> dict:
    name = label("engine", root)
    path = job_path(name, jobs_dir)
    job = plistlib.loads(path.read_bytes()) if path.exists() else None
    loaded = sys.platform == "darwin" and launchctl("print", f"gui/{os.getuid()}/{name}", missing_ok=True)
    selected = selected_release(root)
    pending = bool(_explicit_install(root) or (loaded and (job is None or current_release_from_job(job) != selected)))
    return {"installed": job is not None,
            "loaded": loaded,
            "upgrade_pending": pending,
            "version": job and job.get("EnvironmentVariables", {}).get("CHIEF_OF_STUFF_VERSION"),
            "current": version(current_release())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="workspace root the engine serves")
    ap.add_argument("--release", help=argparse.SUPPRESS)
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
        elif args.cmd == "ensure-retry":
            print(ensure_retry(root))
        elif args.cmd == "commit-handoff":
            if not args.release:
                raise ServiceError("handoff requires a release")
            print(commit_handoff(root, Path(args.release)))
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
