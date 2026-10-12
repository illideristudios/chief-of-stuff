#!/usr/bin/env python3
"""Configure a workspace's forge, Coordinator block and engine settings."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog  # noqa: E402
import issue_gate  # noqa: E402
from backlog_ref import BacklogError, parse_backlog
from install_model_guidance import destination as guidance_destination, install as install_guidance
from workspace import ConfigError, parse_coordinator
from settings import SettingsError, load as load_settings

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COORDINATOR = re.compile(r"(?m)^## Coordinator\s*$")


class InitError(ValueError):
    pass


def relative_dir(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not value or any(part in ("", ".", "..") for part in path.parts):
        raise InitError(f"directory {value!r} must be relative and stay inside the workspace")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise InitError(f"directory {value!r} contains unsupported characters")
    return value.rstrip("/") + "/"


def coordinator_block(*, user: str, timezone: str, daily_dir: str, worktrees: str,
                      agents: list[str], board_port: int | None, backlog: str | None) -> str:
    if not user.strip() or any(ord(c) < 32 or c in "`|" for c in user):
        raise InitError("--user must be one printable line without backticks or pipes")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise InitError(f"unknown timezone {timezone!r}") from None
    daily_dir, worktrees = relative_dir(daily_dir), relative_dir(worktrees)
    if board_port is not None and not 1 <= board_port <= 65535:
        raise InitError("--board-port must be between 1 and 65535")
    if backlog:
        try:
            parse_backlog(backlog)
        except BacklogError as exc:
            raise InitError(str(exc)) from None
    # Tracker paths and calendars remain inputs to current readers during the engine migration.
    # Naming a tracker here creates no tracker tables, mailbox or TOON files.
    lines = ["## Coordinator", "", f"- User: {user.strip()}", f"- Daily log dir: `{daily_dir}`",
             f"- Tracker: `{daily_dir}<date>-tracker.md`", f"- Timezone: {timezone}",
             "- Calendars: none", "- Deadlines: none", f"- Worktrees: `{worktrees}`",
             "- Settings: `chief-of-stuff.toml`"]
    if board_port is not None:
        lines.append(f"- Board: Pages; URL http://127.0.0.1:{board_port}/; dir `pages/`")
    if backlog:
        lines.append(f"- Backlog: {backlog}")
    for agent in agents:
        name, sep, lifetime = agent.partition(":")
        if not sep or not NAME.fullmatch(name) or lifetime not in ("task", "standing"):
            raise InitError(f"--agent {agent!r} must be NAME:task or NAME:standing")
        lines.append(f"- Agent: {name} {lifetime}")
    block = "\n".join(lines) + "\n"
    try:
        parse_coordinator(block, date.today())
    except ConfigError as exc:
        raise InitError(f"generated Coordinator block is invalid: {exc}") from None
    return block


def settings_text(launcher: str, mode: str = "interactive") -> str:
    text = f'[notify]\nadapter = "off"\n\n[workers]\nlauncher = "{launcher}"\nmode = "{mode}"\n\n# [repos]\n# alder = "<path>"  # #54: name -> path, relative to the workspace root; `worktree --clone alder`\n'
    # The engine needs a label for every stage in the shipped workflow before it can start.
    text += ('\n[kanban]\n'
             'stages = ["stage: plan", "stage: implement", "stage: review", "stage: fix", "stage: merge"]\n'
             'human_review_label = "needs human"\n'
             '\n[kanban.map]\nplan = 0\nimplement = 1\nreview = 2\nfix = 3\nmerge = 4\n')
    # #111: uncomment to pick models from the TOML; without [models], chief-of-stuff-models.md applies.
    text += ('\n# [models.deep]\n# rotation = ["claude:opus@high", "codex:<model-id>@high"]\n'
             '# [models.implement]\n# rotation = ["claude:sonnet@medium", "codex:<model-id>@medium"]\n'
             '# [models.fast]\n# rotation = ["claude:haiku@low"]\n'
             '# [models.review]\n# rotation = ["codex:<model-id>@high", "claude:opus@high"]\n')
    text += '\n# [pages]\n# workers = 4\n'
    tomllib.loads(text)
    return text


def _configured_hold_label(settings: Path, new_settings: str) -> str:
    """The hold label the workspace will run with: the existing settings file's, else the new text's (#566)."""
    try:
        data = tomllib.loads(settings.read_text() if settings.exists() else new_settings)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return ""
    kanban = data.get("kanban")
    label = kanban.get("human_review_label", "") if isinstance(kanban, dict) else ""
    return label.strip() if isinstance(label, str) else ""


def provision_hold_label(forge, label: str, dry: bool) -> tuple[str, str]:
    """Create the configured hold label on the forge when it has never seen it, via backlog.ensure_labels (#566).

    Dry-run prints the intent and touches nothing. A forge that refuses is reported and init still succeeds —
    the first hold attempt reports the label again if it never lands.
    """
    if forge is None or not label:
        return "", ""
    where = forge.repo if isinstance(forge, backlog.GitHubBacklog) else forge.project
    if dry:
        return f"would create the hold label {label} on {where}", ""
    try:
        made = backlog.ensure_labels(forge, (label,), commit=True)
    except (BacklogError, OSError) as exc:
        return "", f"hold label not created on {where}: {exc}"
    out = " ".join(f"hold label {w.what} created on {where}" for w in made if w.done)
    pending = "; ".join(w.error for w in made if w.error)
    return out, f"hold label not created on {where}: {pending}" if pending else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="existing workspace directory")
    ap.add_argument("--user", required=True, help="name to show as the workspace user")
    ap.add_argument("--timezone", required=True, help="IANA timezone, e.g. America/Chicago")
    ap.add_argument("--daily-dir", default="daily/")
    ap.add_argument("--worktrees", default="worktrees/")
    ap.add_argument("--agent", action="append", default=[], metavar="NAME:task|standing",
                    help="worker type and lifetime; repeat for multiple types")
    ap.add_argument("--launcher", choices=("ghostty", "tmux"), default="ghostty")
    ap.add_argument("--worker-mode", choices=("interactive", "one-shot"), default="interactive",
                    help="default dispatch mode written to [workers] in the workspace TOML")
    ap.add_argument("--board-port", type=int, help="configure local board serving on this fixed port")
    backlog_group = ap.add_mutually_exclusive_group()
    backlog_group.add_argument("--github-repo", help="GitHub owner/repository")
    backlog_group.add_argument("--gitlab-host", help="GitLab https URL; also pass --gitlab-project")
    ap.add_argument("--gitlab-project", help="GitLab group/repository")
    ap.add_argument("--gitlab-token-env", default="CHIEF_OF_STUFF_GITLAB_TOKEN")
    ap.add_argument("--dry-run", action="store_true", help="print the new block and settings without writing")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        if not root.is_dir():
            raise InitError(f"workspace {root} is not a directory")
        if args.gitlab_host and not args.gitlab_project:
            raise InitError("--gitlab-host requires --gitlab-project")
        if args.gitlab_project and not args.gitlab_host:
            raise InitError("--gitlab-project requires --gitlab-host")
        if not args.github_repo and not args.gitlab_host:
            raise InitError(f"choose --github-repo or --gitlab-host with --gitlab-project\n{issue_gate.SETUP}")
        if not ENV_NAME.fullmatch(args.gitlab_token_env):
            raise InitError("--gitlab-token-env must be an environment variable name")
        backlog_spec = (f"GitHub issues; repo {args.github_repo}" if args.github_repo else
                        f"GitLab; host {args.gitlab_host}; project {args.gitlab_project}; token env {args.gitlab_token_env}"
                        if args.gitlab_host else None)
        block = coordinator_block(user=args.user, timezone=args.timezone, daily_dir=args.daily_dir,
                                  worktrees=args.worktrees, agents=args.agent, board_port=args.board_port,
                                  backlog=backlog_spec)
        claude = root / "CLAUDE.md"
        existing = claude.read_text() if claude.exists() else ""
        if COORDINATOR.search(existing):
            raise InitError(f"{claude} already has a ## Coordinator block")
        settings = root / "chief-of-stuff.toml"
        guidance = guidance_destination(root)
        if settings.exists():
            if not settings.is_file():
                raise InitError(f"{settings} is not a settings file")
            try:
                load_settings(root, settings.name)
            except SettingsError as exc:
                raise InitError(f"existing settings are invalid: {exc}") from None
        new_settings = settings_text(args.launcher, args.worker_mode)
        # #566: with a backlog named, create the [kanban] hold label the workspace will run with, so no
        # review hold ever dies on a label the forge has not seen.
        forge = (backlog.GitHubBacklog(args.github_repo) if args.github_repo else
                 backlog.Backlog(args.gitlab_host, args.gitlab_project) if args.gitlab_host else None)
        label_out, label_err = provision_hold_label(
            forge, _configured_hold_label(settings, new_settings), args.dry_run)
        if args.dry_run:
            print(f"would {'append to' if claude.exists() else 'create'}: {claude}\n{block}")
            print(f"would {'keep existing' if settings.exists() else 'create'}: {settings}")
            if not settings.exists():
                print(new_settings)
            print(f"would {'keep existing' if guidance.exists() else 'create'}: {guidance}")
            if label_out:
                print(label_out)
            return 0
        if not settings.exists():
            with settings.open("x") as fh:
                fh.write(new_settings)
        if claude.exists():
            claude.write_text(existing.rstrip() + "\n\n" + block)
        else:
            with claude.open("x") as fh:
                fh.write("# Workspace\n\n" + block)
        install_guidance(root)
        if label_out:
            print(label_out)
        if label_err:
            print(label_err, file=sys.stderr)
        print(f"configured {root}; settings in {settings}; model guidance in {guidance}")
        return 0
    except (InitError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
