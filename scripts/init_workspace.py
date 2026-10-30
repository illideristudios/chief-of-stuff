#!/usr/bin/env python3
"""Bootstrap configuration inside an existing workspace that has no Coordinator block.

Run it once on a directory that already exists and has no ## Coordinator block.
"""

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


EPILOG = """\
When not to use:
  A workspace that already has a ## Coordinator block is refused. To install or
  keep model guidance on a workspace that is already configured, use
  chief-of-stuff models --root PATH. Init is not an update and not a reset.
  Parent [workspaces] setup (architecture section 12, issue #825) is not
  implemented: init does not read or write that table, register child
  workspaces, or start an engine.

Output:
  Human-readable text. Not TOON and not JSON, and there are no structured
  fields. --help writes this text on stdout and leaves stderr empty.
  A successful write prints, on stdout, an optional "ignored in EXCLUDE" line,
  then optional "hold label" and "stage label" lines for labels the forge
  created, then "configured ROOT; settings in SETTINGS; model guidance in
  GUIDANCE". ROOT, SETTINGS and GUIDANCE are the workspace path and the two
  destination paths.
  --dry-run prints "would append to" or "would create" for the Coordinator
  file and prints the proposed block; "would keep existing" or "would create"
  for settings, and prints new settings only when the file is absent; the same
  keep-or-create line for model guidance, without its contents; and optional
  "would create the hold label" or "would create the stage label" lines, or a
  "would check GitHub Project" line when existing settings name a project.
  A forge that refuses a label prints "hold label not created" or "stage label
  not created" on stderr, and initialization still continues. Configuration and I/O refusals print
  "refused:" on stderr. Usage faults print usage and an error on stderr.

Exit codes:
  0  initialization, --dry-run, or --help succeeded. A label warning on stderr
     is still exit 0.
  1  refused configuration or I/O, including a missing GitLab partner flag, a
     --board-port outside 1-65535, a bad user, timezone, directory, or token
     variable, an existing Coordinator block, and invalid existing settings.
  2  usage fault: a missing required argument, an unknown option, an invalid
     choice, a non-integer --board-port, or both --github-repo and --gitlab-host.

Side effects:
  Reads the workspace directory, an existing Coordinator file, existing
  settings (validated, not rewritten), the shipped model-guidance template
  when guidance is created, and timezone data. Inside a git work tree it also
  reads the exclude file. A real run with a forge reads that forge and may
  create missing labels. --dry-run reads the same local files and does not
  contact the forge.
  Appends one ## Coordinator block to an existing file, keeping the text above
  it and trimming trailing whitespace. Creates the file under a "# Workspace"
  heading when it is absent. An existing Coordinator block is never replaced.
  Creates the settings file only when absent: notifications off, the chosen
  launcher and worker mode, a [kanban] map for the shipped workflow, hold label
  "needs human", and commented examples. Existing valid settings are kept.
  --launcher and --worker-mode change only settings this run creates.
  Creates model guidance only when absent and keeps an existing file.
  Inside a git work tree, appends ignore rules for the new files, the daily and
  worktree directories, and engine state names (store.db and .chief-of-stuff/).
  A Coordinator file is listed only when this run creates it. Other exclude
  lines stay.
  It does not create a store, issues, a repository, daily files, worktrees,
  board files, or a running service, and it does not install or start an
  engine or supervisor, launch a worker, or open a browser. --board-port only
  records the board URL and pages/ directory.
  A forge choice is required. GitHub calls use gh authentication. GitLab
  records the token variable name and uses it for API calls; the token value
  is never written. New settings cause the hold label and one stage label per
  shipped workflow stage to be created when the forge lacks them. Existing
  settings that name a GitHub Project are checked for those options instead of
  creating stage labels; a missing hold label is still created. Those forge
  writes happen immediately. There is no --commit flag. --dry-run only reports
  them. There is no rollback: label provisioning runs before the local writes,
  and a later I/O failure can leave a partial workspace.

Examples:
  The --root directory must already exist: run mkdir -p ROOT first.
  chief-of-stuff init --dry-run --root /tmp/alder --user Robin --timezone America/Chicago --github-repo example-team/alder
  chief-of-stuff init --root /tmp/birch --user Morgan --timezone America/Chicago --github-repo example-team/birch --agent implementer:task --launcher tmux
  chief-of-stuff init --root /tmp/cedar --user Casey --timezone America/Chicago --gitlab-host https://gitlab.example.test --gitlab-project example-team/cedar --gitlab-token-env CEDAR_GITLAB_TOKEN
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="chief-of-stuff init", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)
    ap.add_argument("-h", "--help", action="help",
                    help="Boolean, default false. Print this text on stdout and exit 0. Other arguments are not required.")
    ap.add_argument("--root", type=Path, required=True, metavar="PATH",
                    help="Path, required, no default. An existing workspace directory. init does not create it.")
    ap.add_argument("--user", required=True, metavar="NAME",
                    help="String, required, no default. Coordinator user: one trimmed, nonempty printable line without backticks or pipes.")
    ap.add_argument("--timezone", required=True, metavar="ZONE",
                    help="IANA timezone string, required, no default. Must be a zone ZoneInfo recognizes, for example America/Chicago.")
    ap.add_argument("--daily-dir", default="daily/", metavar="DIR",
                    help="Relative directory string, default daily/. Daily log and tracker paths, kept inside the workspace with a trailing slash. The directory is not created.")
    ap.add_argument("--worktrees", default="worktrees/", metavar="DIR",
                    help="Relative directory string, default worktrees/. Coordinator worktree location, with the same path rules. Worktrees are not created.")
    ap.add_argument("--agent", action="append", default=[], metavar="NAME:task|standing",
                    help="Repeatable string, default none. Append one Agent line. The form is NAME:task or NAME:standing; NAME matches [A-Za-z0-9][A-Za-z0-9._-]{0,63}.")
    ap.add_argument("--launcher", choices=("ghostty", "tmux"), default="ghostty",
                    help="Choice ghostty or tmux, default ghostty. Sets [workers].launcher only in settings this run creates. Starts no terminal.")
    ap.add_argument("--worker-mode", choices=("interactive", "one-shot"), default="interactive",
                    help="Choice interactive or one-shot, default interactive. Sets [workers].mode only in settings this run creates. Dispatches no worker.")
    ap.add_argument("--board-port", type=int, metavar="PORT",
                    help="Integer, default unset. Record a Pages board at http://127.0.0.1:PORT/ with directory pages/. Valid range 1-65535. Nothing is served or opened.")
    backlog_group = ap.add_mutually_exclusive_group()
    backlog_group.add_argument("--github-repo", metavar="OWNER/NAME",
                               help="owner/repository string, default unset. Select a GitHub backlog, mutually exclusive with --gitlab-host. GitHub calls use gh. Required unless --gitlab-host is set.")
    backlog_group.add_argument("--gitlab-host", metavar="URL",
                               help="HTTP or HTTPS URL string, default unset. Select a GitLab backlog, mutually exclusive with --github-repo. Requires --gitlab-project. Required unless --github-repo is set.")
    ap.add_argument("--gitlab-project", metavar="GROUP/NAME",
                    help="Group/project string, default unset. GitLab project path. Requires --gitlab-host, and is refused without it even when --github-repo is set.")
    ap.add_argument("--gitlab-token-env", default="CHIEF_OF_STUFF_GITLAB_TOKEN", metavar="NAME",
                    help="Environment-variable name, default CHIEF_OF_STUFF_GITLAB_TOKEN. Recorded on a GitLab Backlog line and used for GitLab calls. The token value is never written. Checked even for a GitHub backlog.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Boolean, default false. Validate and print the intended file changes and label intent. No file writes and no forge calls.")
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
