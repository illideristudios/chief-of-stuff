#!/usr/bin/env python3
"""Start work under a forge issue, filing it first when needed; preview unless --commit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import issue_gate
import store_access
import supervisor


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
        _, _, source = supervisor.workspace_forge(root)
        if source is None:
            result = issue_gate.start(None, None, args.title)
        elif not args.commit:  # a preview writes nothing, so it never opens or migrates the store
            result = issue_gate.start(source, None, args.title, body=args.body, issue=args.issue)
        else:
            db = store_access.migrated_store(root)
            try:
                result = issue_gate.start(source, db, args.title, body=args.body, issue=args.issue, commit=True)
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
