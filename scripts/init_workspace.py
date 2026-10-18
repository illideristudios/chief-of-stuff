#!/usr/bin/env python3
"""Configure a workspace's forge, Coordinator block and engine settings."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog  # noqa: E402
from forge import project_option_ids  # noqa: E402
import issue_gate  # noqa: E402
from shell_setup import clean_env  # noqa: E402
from backlog_ref import BacklogError, parse_backlog
from install_model_guidance import destination as guidance_destination, install as install_guidance
from workspace import WORKSPACE_FILES, ConfigError, parse_coordinator
from settings import (Kanban, SettingsError, kanban_from_table, load as load_settings, load_policy,
                      unmapped_stages, workflow_stages, workspace_policy)

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


# What the engine writes under a workspace root once it runs, beside init's own files (#870).
ENGINE_STATE = (".chief-of-stuff/", "store.db", "store.db-wal", "store.db-shm")


def ignore_in_git(root: Path, names: list[str], dry: bool) -> str:
    """#870 (Zach, 2026-10-18: "the init files should be gitignored in any repo"): add each name under `root` to its
    repository's info/exclude, which git honours with no tracked change; "" outside a work tree or when all are there."""
    def read(*args: str) -> str:
        # Two path reads need no credentials, so git runs without the write env's (CodeQL: nothing secret reaches print).
        try:
            done = subprocess.run(["git", "-C", str(root), *args], env=clean_env(), capture_output=True, text=True,
                                  timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""
    top, path = read("rev-parse", "--show-toplevel"), read("rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if not top or not path:
        return ""
    prefix = root.resolve().relative_to(Path(top).resolve()).as_posix()
    entries = ["/" + (f"{prefix}/" if prefix != "." else "") + name for name in names]
    exclude = Path(path)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    new = [entry for entry in dict.fromkeys(entries) if entry not in lines]
    if not new:
        return ""
    if dry:
        return f"would ignore in {exclude}: {', '.join(new)}"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join([*lines, *new]) + "\n")
    return f"ignored in {exclude}: {', '.join(new)}"


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
             f"- Settings: `{WORKSPACE_FILES['settings'].name}`"]
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
    stages = workflow_stages(load_policy())
    labels = ", ".join(f'"stage: {s}"' for s in stages)
    mapping = "\n".join(f"{s} = {i}" for i, s in enumerate(stages))
    text += f'\n[kanban]\nstages = [{labels}]\nhuman_review_label = "needs human"\n\n[kanban.map]\n{mapping}\n'
    # #111: uncomment to pick models from the TOML; without [models], chief-of-stuff-models.md applies.
    text += ('\n# [models.deep]\n# rotation = ["claude:opus@high", "codex:<model-id>@high"]\n'
             '# [models.implement]\n# rotation = ["claude:sonnet@medium", "codex:<model-id>@medium"]\n'
             '# [models.fast]\n# rotation = ["claude:haiku@low"]\n'
             '# [models.review]\n# rotation = ["codex:<model-id>@high", "claude:opus@high"]\n')
    text += '\n# [pages]\n# workers = 4\n'
    tomllib.loads(text)
    return text


def _require_engine_ready(config, root: Path) -> None:
    """Preserved settings must already carry a complete shipped workflow map; init does not rewrite them."""
    if config.kanban is None:
        raise InitError("existing settings need [kanban] with the shipped workflow map before the engine can start")
    unmapped = unmapped_stages(config.kanban, workspace_policy(root, config.policy_overlay))
    if unmapped:
        raise InitError(f"existing settings [kanban.map] has no label for workflow stage {', '.join(unmapped)}")


def provision_labels(forge, labels: tuple[str, ...], kind: str, dry: bool) -> tuple[str, str]:
    """Create configured labels on the forge when it has never seen them, via backlog.ensure_labels (#566).

    Dry-run prints the intent and touches nothing. A forge that refuses is reported and init still succeeds —
    the first sync attempt reports the label again if it never lands.
    """
    if forge is None or not labels:
        return "", ""
    where = forge.repo if isinstance(forge, backlog.GitHubBacklog) else forge.project
    if dry:
        return "\n".join(f"would create the {kind} label {label} on {where}" for label in labels), ""
    try:
        made = backlog.ensure_labels(forge, labels, commit=True)
    except (BacklogError, OSError) as exc:
        return "", f"{kind} label not created on {where}: {exc}"
    out = " ".join(f"{kind} label {w.what} created on {where}" for w in made if w.done)
    pending = "; ".join(w.error for w in made if w.error)
    return out, f"{kind} label not created on {where}: {pending}" if pending else ""


def provision_kanban(forge, config: Kanban | None, dry: bool) -> tuple[str, str]:
    """Provision stage and terminal labels, or check the manually created Project options, plus the hold."""
    if forge is None or config is None:
        return "", ""
    hold_out, hold_err = provision_labels(forge, (config.human_review_label,), "hold", dry)
    project = config.github_project if isinstance(forge, backlog.GitHubBacklog) else None
    if project is None:
        stage_out, stage_err = provision_labels(forge, (*config.stages, *config.terminal_stages), "stage", dry)
    else:
        options = (*project.options, *config.terminal_stages)
        stage_out, stage_err = "", ""
        if dry:
            stage_out = (f"would check GitHub Project {project.owner}/{project.number} {project.field} options: "
                         f"{', '.join(options)}; create any missing options before syncing")
        else:
            errors = []
            for option in options:
                _, _, error = project_option_ids(backlog.run_gh, project, option)
                if error:
                    errors.append(error)
            stage_err = "; ".join(errors)
    return "\n".join(x for x in (hold_out, stage_out) if x), "\n".join(x for x in (hold_err, stage_err) if x)


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
        claude = root / WORKSPACE_FILES["config"].name
        existing = claude.read_text() if claude.exists() else ""
        if COORDINATOR.search(existing):
            raise InitError(f"{claude} already has a ## Coordinator block")
        settings = root / WORKSPACE_FILES["settings"].name
        guidance = guidance_destination(root)
        board = None
        if settings.exists():
            if not settings.is_file():
                raise InitError(f"{settings} is not a settings file")
            try:
                existing_settings = load_settings(root, settings.name)
            except SettingsError as exc:
                raise InitError(f"existing settings are invalid: {exc}") from None
            _require_engine_ready(existing_settings, root)
            board = existing_settings.kanban
        new_settings = settings_text(args.launcher, args.worker_mode)
        if board is None:  # a fresh settings file provisions what its own template names
            board = kanban_from_table(tomllib.loads(new_settings)["kanban"])
        # Provision all configured marks, including terminal stages, before the workspace needs to sync them.
        forge = (backlog.GitHubBacklog(args.github_repo) if args.github_repo else
                 backlog.Backlog(args.gitlab_host, args.gitlab_project, args.gitlab_token_env)
                 if args.gitlab_host else None)
        label_out, label_err = provision_kanban(forge, board, args.dry_run)
        # A tracked CLAUDE.md cannot be ignored, so it is listed only when init creates it.
        ignored = [settings.name, guidance.relative_to(root).as_posix(), relative_dir(args.daily_dir),
                   relative_dir(args.worktrees), *ENGINE_STATE, *([] if claude.exists() else [claude.name])]
        if args.dry_run:
            if note := ignore_in_git(root, ignored, dry=True):
                print(note)
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
        if note := ignore_in_git(root, ignored, dry=False):
            print(note)
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
