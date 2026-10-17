"""Independent notification reconciliation and macOS user LaunchAgent lifecycle."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import flow
import notify
from notify import configuration, sync_workspace
from workspace import ConfigError, daily_trackers
from settings import SettingsError
from supervisor import ServiceError, atomic_write, data_dir, ensure_venv, label, load_agent, lock, state_dir, unload_agent

POLL = 5
RECONCILE = 60
RELEASE = Path(__file__).resolve().parent.parent


def instance_lock(root: Path, name: str = "notify-service.lock"):
    return lock(state_dir(root) / name)


def fingerprints(root: Path, now: datetime) -> tuple:
    cfg, _ = configuration(root)
    day = now.astimezone(cfg.zone).date()
    paths = {root / "CLAUDE.md", root / cfg.tracker_path(day.isoformat())}
    if cfg.settings_path:
        paths.add(root / cfg.settings_path)
    paths.update(path for tracker_day, path in daily_trackers(root, cfg) if tracker_day <= day)
    return (day.isoformat(), tuple((str(path), hashlib.sha256(path.read_bytes()).hexdigest()
                                   if path.is_file() else None) for path in sorted(paths)))


class Service:
    def __init__(self, root: Path, reconcile=sync_workspace):
        self.root = root.resolve()
        self.reconcile = reconcile
        self.inputs = None
        self.last_sync = None
        self.last_wall = None
        self.last_tick = None
        self.record = {"pid": os.getpid(), "release": str(RELEASE),
                       "last_success": None, "error": None}

    def tick(self, now: datetime, elapsed: float):
        # A wall/monotonic discrepancy detects sleep and clock adjustments on hosts
        # whose monotonic clock excludes suspended time.
        wall = now.timestamp()
        jumped = (self.last_wall is not None and
                  abs((wall - self.last_wall) - (elapsed - self.last_tick)) >= POLL)
        self.last_wall, self.last_tick = wall, elapsed
        try:
            current = fingerprints(self.root, now)
            if (current == self.inputs and self.record["error"] is None and not jumped
                    and self.last_sync is not None and elapsed - self.last_sync < RECONCILE):
                return
            changes = self.reconcile(self.root, now)
            self.inputs, self.last_sync = current, elapsed
            self.record.update(last_success=now.isoformat(), error=None,
                               disabled=changes.lines == ["notify: off"])
            for line in changes.lines:
                if line != "notify: off":
                    print(line, flush=True)
            try:
                for ev in flow.tick(self.root, now):
                    print(f"flow: {ev}", flush=True)
            except Exception as exc:
                print(f"notify service (flow): {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self.record["error"]:
                print(f"notify service: {message}", file=sys.stderr, flush=True)
            self.record["error"] = message
        atomic_write(state_dir(self.root) / "notify-status.json",
                     json.dumps(self.record).encode())


def serve(root: Path):
    stop_event = threading.Event()
    with instance_lock(root):
        previous = {sig: signal.signal(sig, lambda *_: stop_event.set())
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            worker = Service(root)
            while not stop_event.is_set():
                worker.tick(datetime.now(timezone.utc), time.monotonic())
                stop_event.wait(POLL)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def status(root: Path) -> dict:
    path = state_dir(root) / "notify-status.json"
    record = json.loads(path.read_text()) if path.exists() else {
        "release": None, "last_success": None, "error": None}
    lock = state_dir(root) / "notify-service.lock"
    running = False
    if lock.exists():
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running = True
    return dict(record, running=running)


def require_macos():
    if sys.platform != "darwin":
        raise ServiceError("managed notifications require macOS; run notify serve under your host supervisor")


def ensure(root: Path, jobs_dir: Path | None = None) -> str:
    root = root.resolve()
    _, settings = configuration(root)
    if settings.notify.adapter == "off":
        return "notify: off"
    require_macos()
    with instance_lock(root, "notify-management.lock"):
        directory = state_dir(root)
        data = data_dir()
        with lock(data / "supervisor.lock", wait=True):
            python = ensure_venv(data)
        desired = {"Label": label("notify", root),
                   "ProgramArguments": [str(python),
                                        str(RELEASE / "scripts/notify_service.py"), "--root", str(root), "serve"],
                   "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
                   "ThrottleInterval": 10,
                   "StandardOutPath": str(directory / "notify-service.log"),
                   "StandardErrorPath": str(directory / "notify-service.log")}
        return "notify: " + load_agent(desired, jobs_dir)


def stop(root: Path, jobs_dir: Path | None = None) -> str:
    require_macos()
    with instance_lock(root, "notify-management.lock"):
        unload_agent(label("notify", root), root.resolve(), jobs_dir)
        return "notify: stopped"


SERVICE = ("serve", "ensure", "status", "stop")


def main(argv: list[str] | None = None) -> int:
    """`chief-of-stuff notify`: the service's four commands here, `add` and `sync` in notify.py."""
    argv = sys.argv[1:] if argv is None else argv
    args = iter(argv)
    command = next(args, None)
    while command is not None and (command == "--root" or command.startswith("--root=")):
        if command == "--root":
            next(args, None)
        command = next(args, None)
    if command not in SERVICE:
        return notify.main(argv)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("cmd", choices=SERVICE)
    root = Path(ap.parse_args(argv).root).resolve()
    try:
        if command == "serve":
            serve(root)
        elif command == "status":
            from _vendor.toon_format import encode
            print(encode(status(root)))
        else:
            print(ensure(root) if command == "ensure" else stop(root))
        return 0
    except (OSError, ValueError, RuntimeError, ConfigError, SettingsError, subprocess.TimeoutExpired) as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
