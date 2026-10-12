"""Pure command judgements for the worker guard, using a deliberately limited shell grammar.

No command here is executed. Quotes and shell chains are tokenized; known launch wrappers are unwrapped.
Unsupported syntax fails closed only when the raw text names a guarded program or forge API host.
The guard is a rail, not a sandbox: engine merge verification remains the authority (design 9).
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re
import shlex

OWNED = "The engine owns merges, protected branch pushes, stage labels, Project Status, issue closure and PR readiness."
UNPARSED = "The guard cannot safely parse this command. Use a plain command; " + OWNED
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
REDIRECTIONS = {">", ">>", "<", "<>", ">|", ">&", "<&", "&>", "&>>"}
GUARDED_TEXT = re.compile(r"\b(?:gh|glab|git|hub)\b|\b(?:api\.github\.com|gitlab\.com)\b", re.I)
# Keep operators separate from quoted/escaped words: shlex alone discards that distinction.
SHELL_TOKEN = re.compile(r'''(?P<operator>[;&|()<>\n]+)|(?P<word>(?:[^\s;&|()<>'"\\]|\\.|'[^']*'|"(?:\\.|[^"\\])*")+)''')


class Unparsed(ValueError):
    pass


def _tokens(command: str) -> list[tuple[str, bool]]:
    # Expansion can construct an executable, verb, refspec or a nested command unknown to the gate.
    if not isinstance(command, str) or "$" in command or "`" in command or "\x00" in command:
        raise Unparsed()
    command = command.replace("\\\n", "")
    tokens = []
    end = 0
    previous_word = ""
    for match in SHELL_TOKEN.finditer(command):
        if command[end:match.start()].strip():
            raise Unparsed()
        token = match.group()
        if match.group("operator") is not None:
            # Only an adjacent, unquoted number is a file descriptor (2>file, not 2 >file).
            if token in REDIRECTIONS and match.start() == end and previous_word.isdigit():
                tokens.pop()
            tokens.extend((part, True) for part in re.split(r"(\n)", token) if part)
            previous_word = ""
        else:
            # Preserve comment text; skipping it must not hide the following newline/command.
            tokens.append((shlex.split(token, comments=False, posix=True)[0], False))
            previous_word = token
        end = match.end()
    if command[end:].strip():
        raise Unparsed()
    return tokens


def _unwrap(tokens: list[str]) -> list[str]:
    while tokens:
        name = PurePosixPath(tokens[0]).name
        if ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
        elif name in {"command", "exec", "nohup", "time", "setsid"}:
            tokens = tokens[1:]
            if tokens[:1] == ["--"]:
                tokens = tokens[1:]
            if tokens and tokens[0].startswith("-"):
                raise Unparsed()
        elif name == "timeout":
            tokens = tokens[1:]
            if not tokens or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", tokens[0]):
                raise Unparsed()
            tokens = tokens[1:]
        elif name == "nice":
            tokens = tokens[1:]
            if tokens[:1] == ["-n"]:
                if len(tokens) < 2 or not re.fullmatch(r"-?\d+", tokens[1]):
                    raise Unparsed()
                tokens = tokens[2:]
            elif tokens and tokens[0].startswith("-"):
                raise Unparsed()
        elif name == "env":
            tokens = tokens[1:]
            while tokens:
                flag = tokens[0]
                if ASSIGNMENT.match(flag) or flag in {"-i", "--ignore-environment", "--"}:
                    tokens = tokens[1:]
                elif flag in {"-u", "--unset", "-C", "--chdir"} and len(tokens) > 1:
                    tokens = tokens[2:]
                elif flag.startswith(("--unset=", "--chdir=")):
                    tokens = tokens[1:]
                elif flag.startswith("-"):
                    raise Unparsed()
                else:
                    break
        elif name == "sudo":
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                if tokens[0] in {"-n", "-E", "--"}:
                    tokens = tokens[1:]
                elif tokens[0] in {"-u", "-g"} and len(tokens) > 1:
                    tokens = tokens[2:]
                else:
                    raise Unparsed()
        else:
            return tokens
    return tokens


def _global_args(args: list[str], values: set[str], flags: set[str]) -> list[str]:
    while args and args[0].startswith("-"):
        flag = args[0]
        if flag in values and len(args) > 1:
            args = args[2:]
        elif any(flag.startswith(f"{v}=") for v in values) or flag in flags:
            args = args[1:]
        else:
            raise Unparsed()
    return args


def _push_denied(args: list[str], protected: tuple[str, ...]) -> bool:
    # Explicit destination refspecs only. The repo's push.default, remote config and current branch can change.
    positional = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in {"--all", "--mirror", "--prune", "--follow-tags"}:
            return True
        if token in {"-u", "--set-upstream", "-f", "--force", "--force-with-lease", "--atomic",
                     "--dry-run", "-n", "--delete", "-d", "--porcelain", "--verbose", "-v", "--quiet", "-q", "--"}:
            i += 1
            continue
        if token.startswith("--force-with-lease="):
            i += 1
            continue
        if token.startswith("-"):
            return True
        positional.append(token)
        i += 1
    if len(positional) < 2:
        return True
    for ref in positional[1:]:
        destination = ref.lstrip("+").rsplit(":", 1)[-1].removeprefix("refs/heads/")
        if not destination or destination in {*protected, "HEAD"} or any(c in destination for c in "*?[]~^"):
            return True
    return False


def _forge_denied(args: list[str]) -> bool:
    args = _global_args(args, {"-R", "--repo", "--hostname"}, {"--help", "--version"})
    if not args:
        return False
    group, rest = args[0], args[1:]
    if group == "api":
        # Prefer subcommands. API writes are too general to prove they leave engine-owned state alone.
        method = None
        fields = False
        for i, arg in enumerate(rest):
            if arg in {"-X", "--method"}:
                if i + 1 >= len(rest):
                    raise Unparsed()
                method = rest[i + 1].upper()
            elif arg.startswith(("--method=", "-X")):
                method = arg.split("=", 1)[-1] if "=" in arg else arg[2:]
                method = method.upper()
            if arg in {"-f", "-F", "--field", "--raw-field", "--input"} or arg.startswith(
                    ("--field=", "--raw-field=", "--input=", "-f", "-F")):
                fields = True
        return (method not in {None, "GET", "HEAD"} or fields and method != "GET"
                or any(re.search(r"\bmutation\b", arg, re.I) for arg in rest))
    if group in {"alias", "extension"}:
        raise Unparsed()
    if group not in {"pr", "mr", "issue", "issues", "project", "auth", "repo", "search", "status", "run",
                     "workflow", "release", "label", "help", "version"}:
        raise Unparsed()
    rest = _global_args(rest, {"-R", "--repo"}, set())
    if not rest:
        return False
    verb, flags = rest[0], rest[1:]
    if group in {"pr", "mr"} and verb in {"merge", "ready"}:
        return True
    if group in {"issue", "issues"} and verb in {"close", "delete"}:
        return True
    if group == "project" and verb in {"item-edit", "item-delete", "field-delete", "delete", "close"}:
        return True
    if group in {"issue", "issues", "pr", "mr"}:
        # Stage label names are configurable. Protect all label writes rather than guessing from a prefix.
        return any(flag.split("=", 1)[0] in {"--add-label", "--remove-label", "--label", "--labels", "--unlabel",
                                             "-l", "--ready", "--draft", "--wip", "--state", "--close"}
                   or flag.startswith("-l") and not flag.startswith("--") for flag in flags)
    return False


def _one(tokens: list[str], protected: tuple[str, ...], depth: int) -> bool:
    if depth > 8:
        raise Unparsed()
    tokens = _unwrap(tokens)
    if not tokens:
        return False
    name, args = PurePosixPath(tokens[0]).name, tokens[1:]
    if name in {"sh", "bash", "zsh", "dash", "ksh"}:
        raise Unparsed()
    if name in {"eval", "source", ".", "xargs", "parallel", "if", "then", "else", "fi", "for", "while", "until",
                "do", "done", "case", "function", "{"}:
        raise Unparsed()
    if name in {"python", "python3", "perl", "ruby", "node"} and any(a in {"-c", "-e", "--eval"} for a in args):
        raise Unparsed()
    if name in {"gh", "glab"}:
        return _forge_denied(args)
    if name == "git":
        args = _global_args(args, {"-C", "--git-dir", "--work-tree"}, {"--no-pager", "--paginate"})
        if not args:
            return False
        if args[0] in {"merge", "pull"}:
            return True
        if args[0] == "push":
            return _push_denied(args[1:], protected)
        # An unknown subcommand can be an alias containing an engine-owned verb.
        if args[0] not in {"status", "diff", "log", "show", "rev-parse", "symbolic-ref", "branch", "checkout", "switch",
                           "add", "commit", "restore", "reset", "fetch", "pull", "rebase", "cherry-pick", "stash", "tag",
                           "ls-files", "ls-tree", "cat-file", "grep", "blame", "remote", "config", "worktree", "help",
                           "init", "clone", "describe", "reflog", "clean", "check-ignore", "update-index", "diff-tree"}:
            raise Unparsed()
    # Unrecognised launchers may execute the remainder (e.g. docker exec). Inspect visible nested
    # commands, but never treat an unknown wrapper as proof that a sensitive write is safe.
    if name not in {"echo", "printf"}:
        for i, token in enumerate(args):
            if PurePosixPath(token).name in {"gh", "glab", "git", "sh", "bash", "zsh", "dash", "eval"}:
                if _one(args[i:], protected, depth + 1):
                    raise Unparsed()
    return False


def _denied(command: str, protected: tuple[str, ...], depth: int) -> bool:
    if depth > 8:
        raise Unparsed()
    tokens, segment = _tokens(command), []
    i = 0
    tokens.append((";", True))
    while i < len(tokens):
        token, operator = tokens[i]
        i += 1
        if operator and token in REDIRECTIONS:
            # Redirection destinations are not command arguments.
            if i >= len(tokens) or tokens[i][1]:
                raise Unparsed()
            i += 1
        elif operator and token in SEPARATORS:
            if segment and _one(segment, protected, depth):
                return True
            segment = []
        elif operator:
            # Groups, heredocs and malformed shell operators are outside this grammar.
            raise Unparsed()
        else:
            segment.append(token)
    return False


def denial_reason(command: str, protected_branches: tuple[str, ...]) -> str | None:
    try:
        return OWNED if _denied(command, protected_branches, 0) else None
    except (ValueError, TypeError, RecursionError):
        return UNPARSED if not isinstance(command, str) or GUARDED_TEXT.search(command) else None
