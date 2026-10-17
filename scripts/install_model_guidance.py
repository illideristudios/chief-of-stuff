#!/usr/bin/env python3
"""Copy the editable worker model guidance template into a workspace once, or, with --class, print the
entry of that task class's `[models]` rotation in the Settings TOML (#111)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "worker-model-guidance.md"
NAME = "chief-of-stuff-models.md"


def destination(root: Path) -> Path:
    if not root.is_dir():
        raise ValueError(f"workspace {root} is not a directory")
    path = root / NAME
    if path.exists() and not path.is_file():
        raise ValueError(f"{path} exists but is not a file")
    return path


def install(root: Path, *, dry_run: bool = False) -> tuple[Path, bool]:
    """Return (path, created); preserve every existing workspace edit."""
    path = destination(root)
    if path.exists():
        return path, False
    if not dry_run:
        body = TEMPLATE.read_text()
        with path.open("x") as output:
            output.write(body)
    return path, True


def chosen(root: Path, name: str, after: str | None, not_family: str | None) -> str:
    from settings import load, pick
    from workspace import ConfigError, read_config
    try:
        cfg = read_config(root)
    except (OSError, ConfigError) as exc:
        raise ValueError(f"cannot read the ## Coordinator block: {exc}") from None
    models = load(root, cfg.settings_path).models
    if not models:
        raise ValueError(f"the Settings TOML has no [models]; choose from {NAME}")
    if name not in models:
        raise ValueError(f"no [models.{name}] in the Settings TOML; classes: {', '.join(models)}")
    return str(pick(models[name], after, not_family))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--class", dest="model_class", help="print this task class's suggested runtime:model-id@effort")
    ap.add_argument("--after", metavar="RUNTIME:ID", help="with --class: the next entry after this one")
    ap.add_argument("--not-family", metavar="RUNTIME", help="with --class: skip this runtime's entries")
    args = ap.parse_args(argv)
    if args.model_class:
        try:
            print(chosen(args.root.resolve(), args.model_class, args.after, args.not_family))
        except (OSError, ValueError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        path, created = install(args.root.resolve(), dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{'would create' if args.dry_run and created else 'created' if created else 'kept existing'}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
