#!/usr/bin/env python3
"""Start work under a forge issue, filing it first when needed; preview unless --commit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import forge
import issue_gate
import settings
import supervisor
from store import Store, migrate
from workspace import settings_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("title", help="title of the issue to file")
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace holding CLAUDE.md and store.db")
    ap.add_argument("--body", default="", help="body of a new issue")
    ap.add_argument("--issue", type=int, help="start under an existing issue instead of filing one")
    ap.add_argument("--commit", action="store_true", help="file the issue and write its store row")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        claude = root / "CLAUDE.md"
        text = claude.read_text() if claude.is_file() else ""
        config = settings.load(root, settings_path(text))
        source = forge.for_workspace(root, approver=config.workflow.approver or "", kanban=config.kanban)
        if source is None:
            result = issue_gate.start(None, None, args.title)
        else:
            path = root / "store.db"
            # The live engine holds engine.lock for its whole run; migrations take their own short lock.
            with supervisor.lock(supervisor.state_dir(root) / "migrate.lock", wait=True):
                migrate(path)
            db = Store(path)
            try:
                result = issue_gate.start(source, db, args.title, body=args.body,
                                          issue=args.issue, commit=args.commit)
            finally:
                db.engine.dispose()
        if result.refused:
            print(f"refused: {result.refused}", file=sys.stderr)
            if result.setup:
                print(result.setup, file=sys.stderr)
            return 1
        print(result.preview or f"started: {result.issue_key}")
        return 0
    except Exception as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
