"""Pure command judgements for the worker guard, using a deliberately limited shell grammar.

No command here is executed. Quotes and shell chains are tokenized; known launch wrappers are unwrapped.
Unsupported syntax fails closed when the raw text names a guarded program, forge API host or inline interpreter.
The guard is a rail, not a sandbox: engine merge verification remains the authority (design 9).
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import os
from pathlib import PurePosixPath
import re
import shlex

OWNED = "The engine owns merges, protected branch pushes, stage labels, Project Status, issue closure, PR readiness and review verdicts; forge commands outside the worker allowlist are denied."
UNPARSED = "The guard cannot safely parse this command. Use a plain command; " + OWNED
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
REDIRECTIONS = {">", ">>", "<", "<>", ">|", ">&", "<&", "&>", "&>>"}
# Enterprise forge hosts can have arbitrary names, so also recognise their standard API paths.
FORGE_API = re.compile(r"\b(?:api|uploads)\.github\.com\b|/api/(?:graphql|v3|v4)\b", re.I)
GUARDED_TEXT = re.compile(r"\b(?:gh|glab|git|hub)\b|\bgitlab\.com\b|" + FORGE_API.pattern, re.I)
# Keep operators separate from quoted/escaped words: shlex alone discards that distinction.
SHELL_TOKEN = re.compile(r'''(?P<operator>[;&|()<>\n]+)|(?P<word>(?:[^\s;&|()<>'"\\]|\\.|'[^']*'|"(?:\\.|[^"\\])*")+)''')
# Versioned names, attached flags and bundles share one table in parsing and the raw-text fallback.
INTERPRETERS = ((r"(?:python|pypy|perl|ruby|node(?:js)?)[\d.]*", "cep"), (r"php[\d.]*", "rBRE"),
                (r"(?:lua(?:jit)?|julia|swift|Rscript|osascript|deno)[\d.]*", "e"),
                (r"bun[\d.]*", "ep"),
                (r"pwsh|powershell", "cC"))
# Only known reads and worker writes may reach a forge. New CLI verbs fail closed.
FORGE_ALLOWED = {
    "gh": {
        "pr": {"view", "list", "diff", "checks", "status", "create", "comment", "review", "checkout"},
        "issue": {"view", "list", "status", "comment"},
        "run": {"view", "list", "watch"},
        "workflow": {"view", "list"},
        "repo": {"view", "clone"},
        "release": {"view", "list"},
        "label": {"list"},
        "project": {"view", "list", "item-list", "field-list"},
        "auth": {"status"},
    },
    "glab": {
        "mr": {"view", "list", "diff", "create", "note", "checkout"},
        "issue": {"view", "list", "note"},
        "ci": {"view", "list", "status", "trace"},
        "repo": {"view", "clone"},
        "label": {"list"},
        "auth": {"status"},
    },
}


class Unparsed(ValueError):
    pass


class Opaque(Unparsed):
    """A program the gate cannot read, such as an inline interpreter program: denied even when no guarded word shows."""


class ImplicitPush(ValueError):
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


def _assignment(token: str) -> bool:
    if not ASSIGNMENT.match(token):
        return False
    # GIT_SSH_COMMAND, GIT_EXTERNAL_DIFF, GIT_CONFIG_PARAMETERS and the like run their value inside allowed verbs.
    name, value = token.split("=", 1)
    # Git reads configuration from GIT_CONFIG_COUNT/KEY_n/VALUE_n and GIT_CONFIG_PARAMETERS; config is read-only here.
    if name in {"GIT_ALLOW_PROTOCOL", "GIT_PROTOCOL_FROM_USER", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
                "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"} or name.startswith(("GIT_CONFIG_KEY_",
                                                                                          "GIT_CONFIG_VALUE_")):
        raise Opaque()
    if re.search(r"\s", value) and NESTED_WORD.search(value):
        raise Unparsed()
    return True


def _unwrap(tokens: list[str]) -> list[str]:
    while tokens:
        name = PurePosixPath(tokens[0]).name
        if _assignment(tokens[0]):
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
                if _assignment(flag) or flag in {"-i", "--ignore-environment", "--"}:
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


def _config_denied(args: list[str]) -> bool:
    action = args[0] if args and args[0] in {"get", "list"} else None
    if action:
        args = args[1:]
    flags = {"--global", "--system", "--local", "--worktree", "--includes", "--no-includes",
             "--show-origin", "--show-scope", "--name-only", "--null", "-z", "--all", "--regexp",
             "--fixed-value", "--bool", "--int", "--bool-or-int", "--path", "--expiry-date"}
    values = {"--file", "-f", "--blob", "--type", "-t", "--default", "--value", "--url"}
    # Git stops parsing legacy options at the first positional name. Consume option values too:
    # a file named --get is not a read action.
    while args and args[0].startswith("-"):
        flag = args[0]
        if flag in {"--get", "--get-all", "--get-regexp", "--list", "-l"} and action is None:
            action = flag
            args = args[1:]
        elif flag in flags or any(flag.startswith(f"{v}=") for v in values if v.startswith("--")):
            args = args[1:]
        elif flag in values and len(args) > 1:
            args = args[2:]
        else:
            return True
    if action in {"--list", "-l", "list"}:
        return bool(args)
    return (action is None or len(args) not in ({1} if action == "get" else {1, 2})
            or any(arg.startswith("-") for arg in args))


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
        # Protected names may be forge wildcards such as release/*.
        if (not destination or destination == "HEAD" or any(c in destination for c in "*?[]~^")
                or any(fnmatchcase(destination, pattern) for pattern in protected)):
            return True
    return False


def _glab_note_denied(flags: list[str]) -> bool:
    """Quick actions in note text mutate forge state; file-sourced notes are opaque."""
    i = 0
    while i < len(flags):
        flag = flags[i]
        i += 1
        if flag in {"--file", "-F"} or flag.startswith(("--file=", "-F=")):
            return True
        if flag in {"-m", "--message"}:
            value = flags[i] if i < len(flags) else ""
            i += 1
        elif flag.startswith(("--message=", "-m=")):
            value = flag.split("=", 1)[1]
        else:
            continue
        if GLAB_QUICK_ACTION.search(value):
            return True
    return False


def _glab_mr_create_description_denied(flags: list[str]) -> bool:
    """Quick actions in MR descriptions mutate forge state; file-sourced descriptions are opaque."""
    i = 0
    while i < len(flags):
        flag = flags[i]
        i += 1
        if flag == "--description-file" or flag.startswith("--description-file="):
            return True
        if flag in {"-d", "--description"}:
            value = flags[i] if i < len(flags) else ""
            i += 1
        elif flag.startswith(("--description=", "-d=")):
            value = flag.split("=", 1)[1]
        else:
            continue
        if value == "-" or GLAB_QUICK_ACTION.search(value):
            return True
    return False


def _pr_create_denied(flags: list[str], protected: tuple[str, ...]) -> bool:
    # Consume value flags so a title or body containing --head cannot stand in for the branch option.
    values = {"--head", "-H", "--title", "-t", "--body", "-b", "--body-file", "-F", "--base", "-B",
              "--assignee", "-a", "--reviewer", "-r", "--milestone", "-m", "--template", "-T",
              "--recover", "--repo", "-R", "--attach", "--project", "-p", "--label", "-l"}
    head, help_requested = None, False
    i = 0
    while i < len(flags):
        flag = flags[i]
        i += 1
        if flag == "--":
            break
        if flag in values:
            value = flags[i] if i < len(flags) else ""
            i += 1
        elif flag.startswith("--") and "=" in flag:
            flag, value = flag.split("=", 1)
        else:
            help_requested |= flag in {"--help", "-h"}
            continue
        if flag in {"--head", "-H"}:
            head = value
    if help_requested:
        return False
    if not head or not head.strip():
        raise ImplicitPush()
    return _push_denied(["origin", head], protected)


def _forge_denied(args: list[str], client: str, protected: tuple[str, ...], depth: int) -> bool:
    client_args = args
    args = _global_args(args, {"-R", "--repo", "--hostname"}, {"--help", "--version"})
    if not args:
        return False
    group, rest = args[0], args[1:]
    if group == "repo":
        rest = _global_args(rest, {"-R", "--repo"}, set())
        if rest[:1] == ["clone"] and "--" in rest:
            # Both forge clients forward everything after -- to git clone, including execution configuration.
            forwarded = rest[rest.index("--") + 1:]
            if _one(["git", "clone", *forwarded], protected, depth + 1):
                return True
    # Bundles can hide protected flags; require separate short flags and values or long forms.
    if any(arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 for arg in client_args):
        return True
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
    if client == "gh" and group in {"search", "status", "help", "version"}:
        return False
    allowed = FORGE_ALLOWED[client].get(group, set())
    if not allowed:
        return True
    rest = _global_args(rest, {"-R", "--repo"}, set())
    if not rest:
        return True
    verb, flags = rest[0], rest[1:]
    if verb not in allowed:
        return True
    if group in {"issue", "pr", "mr"} and verb in {"comment", "note"}:
        # The shared identity's earlier comments and discussion state belong to the user.
        if any(flag.split("=", 1)[0] in {"--delete-last", "--edit-last", "--create-if-none",
                                       "--resolve", "--unresolve"} for flag in flags):
            return True
        if client == "glab" and verb == "note" and _glab_note_denied(flags):
            return True
        if client == "glab" and group == "mr":
            # glab accepts note flags before its nested verb; skip their values, including message text.
            note_args = _global_args(flags, {"-R", "--repo", "-m", "--message", "--attach", "--reply",
                                             "--file", "--line", "--old-line"},
                                    {"--help", "-h", "--yes", "-y", "--unique", "--internal",
                                     "--resolvable", "--resolvable=true", "--resolvable=false"})
            if note_args and note_args[0] in {"delete", "update", "resolve", "reopen", "draft"}:
                return True
    if group == "pr" and verb == "review":
        # Approval is the user's and a change request holds the merge (reconcile); findings go through the report.
        if any(flag.split("=", 1)[0] in {"--approve", "--request-changes"}
               or flag.startswith(("-a", "-r")) and not flag.startswith("--") for flag in flags):
            return True
        if not any(flag in {"--comment", "-c"} for flag in flags):
            return True
    if group in {"issue", "issues", "pr", "mr"}:
        # Stage label names are configurable. Protect all label writes rather than guessing from a prefix.
        if any(flag.split("=", 1)[0] in {"--add-label", "--remove-label", "--label", "--labels", "--unlabel",
                                       "-l", "--ready", "--draft", "--wip", "--state", "--close"}
               or flag.startswith("-l") and not flag.startswith("--")
               or client == "gh" and verb in {"create", "edit"}
               and flag.split("=", 1)[0] in {"--project", "-p", "--add-project", "--remove-project"}
               for flag in flags):
            return True
    if client == "gh" and group == "pr" and verb == "create":
        return _pr_create_denied(flags, protected)
    if client == "glab" and group == "mr" and verb == "create":
        # --fill also sets push to true in glab.
        return (any(flag.split("=", 1)[0] in {"--push", "--fill", "-f"} for flag in flags)
                or _glab_mr_create_description_denied(flags))
    return False


NESTED_WORD = re.compile(r"(?<![\w.-])(?:gh|glab|git|hub|sh|bash|zsh|dash|eval)(?![\w.])")
# GitLab quick actions run from note text; a leading /word on a line changes forge state.
GLAB_QUICK_ACTION = re.compile(r"(?m)^\s*/\w+")


def _inline_interpreter(name: str, args: list[str]) -> bool:
    return any(re.fullmatch(names, name) and any(
        a.split("=", 1)[0] in {"--eval", "--print", "--run", "--process-begin", "--process-code", "--process-end"}
        or a == "eval"  # deno eval, bun eval
        or a.startswith("-") and not a.startswith("--") and re.search(f"[{letters}]", a) for a in args)
        for names, letters in INTERPRETERS)


def _inline_interpreter_text(command: str) -> bool:
    # Parsing already failed: scan conservatively, including wrappers, broken quotes and quoted/escaped flags.
    # These words cannot prove a command safe; they only recognise opaque code without a literal forge name.
    text = command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\"))
    words = re.findall(r"[^\s;&|()<>`]+", text)
    return any(_inline_interpreter(PurePosixPath(word).name, words[i + 1:]) for i, word in enumerate(words))


def _one(tokens: list[str], protected: tuple[str, ...], depth: int) -> bool:
    if depth > 8:
        raise Unparsed()
    tokens = _unwrap(tokens)
    if not tokens:
        return False
    name, args = PurePosixPath(tokens[0]).name, tokens[1:]
    if name.startswith("git-"):
        name, args = "git", [name[4:], *args]
    if name == "hub":
        return True  # a git wrapper with forge verbs of its own: workers use git and gh
    # Plain clients bypass the forge CLI gates. Workers must use gh/glab for forge reads too.
    if name not in {"echo", "printf"} and any(FORGE_API.search(arg) for arg in args):
        return True
    if name in {"sh", "bash", "zsh", "dash", "ksh"}:
        raise Unparsed()
    if name in {"eval", "source", ".", "xargs", "parallel", "if", "then", "else", "fi", "for", "while", "until",
                "do", "done", "case", "function", "{"}:
        raise Unparsed()
    # Versioned names too: python3.14, pypy3, perl5.36, nodejs. A program can assemble `gh` at runtime, so no word check.
    # Attached and bundled forms too: python3 -c'...', perl -ne, node -p. Each entry: names, inline-code short flags.
    if _inline_interpreter(name, args):
        raise Opaque()
    if re.fullmatch(r"[gmn]?awk", name) and not any(a == "-f" or a.startswith(("-f", "--file")) for a in args):
        raise Opaque()  # the first operand is an inline program with system()
    if name in {"gh", "glab"}:
        return _forge_denied(args, name, protected, depth)
    if name == "git":
        args = _global_args(args, {"-C", "--git-dir", "--work-tree"}, {"--no-pager", "--paginate"})
        if not args:
            return False
        if args[0] in {"merge", "pull"}:
            return True
        if any(re.match(r"(?i)(?:ext|fd)::", a) for a in args):
            raise Opaque()  # ext:: transports run a command (git-remote-ext), for push as for fetch
        if args[0] == "push":
            return _push_denied(args[1:], protected)
        if args[0] == "rebase" and any(a.split("=", 1)[0] == "--exec"
                                     or a.startswith("-") and not a.startswith("--") and "x" in a
                                     for a in args[1:]):
            raise Opaque()
        if args[0] == "clone" and any(a.split("=", 1)[0] in {"-c", "--config"} or a.startswith("-c")
                                      and not a.startswith("--") for a in args[1:]):
            raise Opaque()  # core.sshCommand and friends run during the clone itself
        if args[0] == "config":
            # Values such as core.sshCommand, alias.* and *.helper run later inside allowed verbs. Reads only.
            return _config_denied(args[1:])
        if args[0] == "grep" and any(
                a.split("=", 1)[0] in {"-O", "--open-files-in-pager"}
                or a.startswith("-") and not a.startswith("--") and "O" in a for a in args[1:]):
            raise Opaque()  # -O/--open-files-in-pager runs a pager command on matches
        # An unknown subcommand can be an alias containing an engine-owned verb.
        if args[0] not in {"status", "diff", "log", "show", "rev-parse", "symbolic-ref", "branch", "checkout", "switch",
                           "add", "commit", "restore", "reset", "fetch", "pull", "rebase", "cherry-pick", "stash", "tag",
                           "ls-files", "ls-tree", "cat-file", "grep", "blame", "remote", "config", "worktree", "help",
                           "init", "clone", "describe", "reflog", "clean", "check-ignore", "update-index", "diff-tree"}:
            raise Unparsed()
    # Unrecognised launchers may execute the remainder (e.g. runner exec box). Inspect visible nested
    # commands, but never treat an unknown wrapper as proof that a sensitive write is safe.
    if name not in {"echo", "printf", "git"}:
        for token in args:
            # A launcher can take the whole command as one argument: ssh host 'gh pr merge 12', or after an option
            # prefix: tar --checkpoint-action=exec='gh pr merge 12'. Read it from each named program too.
            if re.search(r"\s", token) and NESTED_WORD.search(token) and any(
                    _denied(token[start:], protected, depth + 1)
                    for start in (0, *(m.start() for m in NESTED_WORD.finditer(token)))):
                raise Unparsed()
    if name not in {"echo", "printf"}:
        for i, token in enumerate(args):
            nested_name = PurePosixPath(token).name
            if (nested_name in {"gh", "glab", "git", "hub", "sh", "bash", "zsh", "dash", "eval"}
                    or nested_name.startswith("git-")):
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


# #874: programs a PermissionRequest may allow without anyone, because they only read. Anything else goes to the trio.
READERS = {"ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "ag", "sort", "uniq", "cut", "tr", "file",
           "stat", "tree", "jq", "yq", "du", "df", "pwd", "echo", "printf", "which", "basename", "dirname", "realpath",
           "readlink", "diff", "cmp", "comm", "nl", "column", "true", "false", "test", "[", "find"}
GIT_READS = {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "cat-file", "grep", "blame",
             "describe", "shortlog", "show-ref", "merge-base", "name-rev", "rev-list", "for-each-ref", "branch", "remote"}
FORGE_READS = {"view", "list", "diff", "checks", "status", "item-list", "field-list", "trace"}
RTK_READS = {"ls", "read", "grep", "find", "tree", "wc", "diff", "json", "deps", "env", "summary"}
# #890: `sed -n` with a script of line-range prints only (`N`, `N,M`, `N,$`, then `p`): sed's w, e and r write, run
# and read, and -i edits in place, so nothing else of sed is a read.
SED_PRINTS = re.compile(r"\s*\d+(?:,(?:\d+|\$))?p(?:\s*;\s*\d+(?:,(?:\d+|\$))?p)*\s*;?\s*")


def _segments(command: str) -> list[tuple[list[str], list[str]]]:
    """(words, redirect targets) per simple command, by the same tokenizer and separators the gate uses."""
    tokens, out, words, targets, i = _tokens(command), [], [], [], 0
    tokens.append((";", True))
    while i < len(tokens):
        token, operator = tokens[i]
        i += 1
        if operator and token in REDIRECTIONS:
            if i >= len(tokens) or tokens[i][1]:
                raise Unparsed()
            targets.append(tokens[i][0])
            i += 1
        elif operator and token in SEPARATORS:
            if words or targets:
                out.append((words, targets))
            words, targets = [], []
        elif operator:
            raise Unparsed()
        else:
            words.append(token)
    return out


def inside(path: str, cwd: str | None) -> bool:
    """Whether a path argument stays in the worktree; a word that is not a path (no /, ~ or ..) is inside."""
    if not path.startswith(("/", "~")) and ".." not in PurePosixPath(path).parts:
        return True
    if not cwd or path.startswith("~"):
        return False  # a home path is outside any worktree; the guard does not expand it
    target = os.path.normpath(os.path.join(cwd, path))  # lexical only: this module reads no files
    root = os.path.normpath(cwd)
    return target == root or target.startswith(root + os.sep)


def _reads_only(words: list[str]) -> bool:
    name, args = PurePosixPath(words[0]).name, words[1:]
    if name == "rtk":
        if not args:
            return False
        if args[0] == "proxy":
            return bool(args[1:]) and _reads_only(args[1:])
        if args[0] == "git":
            return _reads_only(args)
        return args[0] in RTK_READS
    if name == "git":
        # Options that run a configured or given program, even on a read verb (architect audit on #875, hole 2).
        if any(a in {"-c", "-O", "--ext-diff", "--textconv"} or a.startswith(("-c", "--config-env", "--exec-path",
                                                                               "--open-files-in-pager"))
               or a.startswith("-") and not a.startswith("--") and "O" in a for a in args):
            return False
        args = _global_args(args, {"-C", "--git-dir", "--work-tree"}, {"--no-pager", "--paginate"})
        if not args or args[0] not in GIT_READS:
            return False
        if args[0] == "branch" and any(a in {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy",
                                             "-u", "--set-upstream-to", "--unset-upstream", "-f", "--force"}
                                       or not a.startswith("-") for a in args[1:]):
            return False  # listing only: a name argument creates a branch
        if args[0] == "remote" and args[1:] and args[1] not in {"-v", "--verbose", "show", "get-url"}:
            return False
        return True
    if name == "sed":
        return len(args) >= 2 and args[0] == "-n" and bool(SED_PRINTS.fullmatch(args[1])) and not any(
            a.startswith("-") for a in args[2:])
    if name in {"gh", "glab"}:
        return len(args) >= 2 and args[1] in FORGE_READS
    if name not in READERS:
        return False
    if name == "find" and any(a in {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf",
                                    "-fls"} for a in args):
        return False
    if name in {"rg", "ag"} and any(a.split("=", 1)[0] in {"--pre", "--pre-glob"} for a in args):
        return False  # --pre runs a preprocessor command on every file
    return True


# #890: a match pattern names what to skip or select, not a path the command reads or writes.
FIND_PATTERNS = {"-path", "-ipath", "-name", "-iname", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname"}
GREP_PATTERNS = ("--exclude", "--exclude-dir", "--include", "--glob", "--iglob")


def _patterns(words: list[str]) -> set[int]:
    """The indexes of `words` that are find or grep match patterns, which the path check leaves alone."""
    args = words[1:] if PurePosixPath(words[0]).name != "rtk" else words[2:]
    offset = len(words) - len(args)
    name = PurePosixPath(words[offset - 1]).name if offset else ""
    hits = set()
    for i, arg in enumerate(args, offset):
        if name == "find" and i > offset and words[i - 1] in FIND_PATTERNS:
            hits.add(i)
        elif name in {"grep", "egrep", "fgrep", "rg", "ag"} and arg.startswith(GREP_PATTERNS):
            hits.add(i)
        elif name in {"rg", "ag"} and i > offset and words[i - 1] in {"-g", "--glob", "--iglob"}:
            hits.add(i)
    if name in {"grep", "egrep", "fgrep", "rg", "ag"} and not any(a in {"-e", "-f", "--regexp", "--file"}
                                                                   or a.startswith(("--regexp=", "--file=")) for a in args):
        # Without -e or -f, the first operand is the pattern; the rest are paths.
        first = next((i for i, a in enumerate(args, offset) if not a.startswith("-") and i not in hits), None)
        if first is not None:
            hits.add(first)
    return hits


def _tree_write(words: list[str]) -> bool:
    """#890: git writes confined to the worker's own tree and branch: `add` (its paths are checked in-tree after),
    `commit` without --amend or --no-verify, and `switch -c` / `checkout -b` creating a new branch. The gate's denials
    (protected branches, force, history rewrites) are checked before this."""
    if PurePosixPath(words[0]).name != "git":
        return False
    args = words[1:]
    leading = args[:next((i for i, a in enumerate(args) if not a.startswith("-")), len(args))]  # git's own options
    if any(a.startswith(("-c", "--config-env", "--exec-path")) for a in leading) or any(
            a in {"--ext-diff", "--textconv"} for a in args):
        return False  # a configured program would run, as for reads
    if any(a in {"-C", "--git-dir", "--work-tree"} or a.startswith(("--git-dir=", "--work-tree=")) for a in leading):
        return False  # a write aimed at another repository or tree
    try:
        args = _global_args(args, set(), {"--no-pager"})
    except Unparsed:
        return False
    if not args:
        return False
    verb, rest = args[0], args[1:]
    if verb == "add":
        return not any(a in {"--interactive", "-i", "--patch", "-p", "--edit", "-e"} for a in rest)
    if verb == "commit":
        # -n is --no-verify, also inside a cluster such as -an; a message may follow -m.
        flags = [a for i, a in enumerate(rest) if a.startswith("-") and not (i and rest[i - 1] in {"-m", "--message"})]
        return not any(a in {"--amend", "--no-verify", "--fixup", "--squash"} or a.startswith(("--amend", "--fixup=", "--squash="))
                       or (not a.startswith("--") and "n" in a[1:]) for a in flags)
    if verb in {"switch", "checkout"}:
        return len(rest) == 2 and rest[0] == ("-c" if verb == "switch" else "-b") and not rest[1].startswith("-")
    if verb == "worktree":
        return rest[:1] == ["list"] and all(a in {"--porcelain", "-v", "--verbose", "-z"} for a in rest[1:])
    return False


def _accepted_commit(words: list[str]) -> bool:
    if PurePosixPath(words[0]).name != "git":
        return False
    try:
        args = _global_args(words[1:], set(), {"--no-pager"})
    except Unparsed:
        return False
    return bool(args) and args[0] == "commit"


def _commit_hooks(words: list[str], hooks_path: str | None, hooks_root: str | None) -> tuple[str, str] | None:
    """#893: an auto-allowed commit runs hooks, so a core.hooksPath inside the worktree (or one this judge could not
    read) is not auto-allowed. Unset stays a tree write. One commit segment sends the whole command to the trio."""
    if hooks_path is None or not _tree_write(words) or not _accepted_commit(words):
        return None
    if not hooks_root:  # before inside: inside(absolute, "") is false, which would allow an unreadable root
        return "trio", "core.hooksPath could not be resolved"
    if inside(hooks_path, hooks_root):
        return "trio", "core.hooksPath is inside the worktree"
    return None


def judgement(command: str, protected_branches: tuple[str, ...], cwd: str | None,
              hooks_path: str | None = None, hooks_root: str | None = None) -> tuple[str, str]:
    """#874: a PermissionRequest's answer for a Bash command: ("deny", reason) for what the gate refuses, ("allow", "")
    for a fully parsed command of read-only programs whose paths and redirects stay in the worktree (/dev/null and file
    descriptors are not writes), else ("trio", why). This never overrules Claude's own layer for a write or a secret read."""
    reason = denial_reason(command, protected_branches)
    if reason:
        return "deny", reason
    try:
        segments = _segments(command)
    except (ValueError, TypeError, RecursionError):
        return "trio", "the guard cannot parse this command"
    for raw, targets in segments:
        words = _unwrap(list(raw)) if raw else []
        # A VAR=value prefix (GIT_PAGER, GIT_EXTERNAL_DIFF, GH_PAGER...) can make a reader run a program (hole 1).
        if any(ASSIGNMENT.match(word) for word in raw[:len(raw) - len(words)]):
            return "trio", "an environment assignment prefixes the command"
        # Brace expansion builds paths the lexical check never sees: cat {.,/etc}/passwd (hole 3).
        if any("{" in word for word in [*raw, *targets]):
            return "trio", "a brace expansion can name a path outside the worktree"
        if words and not (_reads_only(words) or _tree_write(words)):
            return "trio", f"{PurePosixPath(words[0]).name} is not a known read-only command"
        if words:
            refused = _commit_hooks(words, hooks_path, hooks_root)
            if refused:
                return refused
        patterns = _patterns(words) if words else set()
        for i, arg in enumerate(words[1:], 1):
            if i in patterns:
                continue
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if not inside(value, cwd) or _in_git_dir(value):
                return "trio", f"{value} is outside the worktree or in its .git"
        for target in targets:
            if target != "/dev/null" and not target.isdigit() and (not inside(target, cwd) or _in_git_dir(target)):
                return "trio", f"a redirect to {target} is outside the worktree or in its .git"
    return "allow", ""


def _in_git_dir(path: str) -> bool:
    """A .git component: in a linked worktree <cwd>/.git is in-tree, and rewriting it redirects the engine's later
    credentialed git calls (architect audit on #875, hole 4)."""
    return ".git" in PurePosixPath(path).parts


READ_TOOLS = {"Read", "Glob", "Grep", "LS"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def permission_judgement(event: dict, protected_branches: tuple[str, ...]) -> tuple[str, str]:
    """#874: ("allow", ""), ("deny", reason) or ("trio", why) for a PermissionRequest. Bash is judged by guard_commands,
    as the PreToolUse gate judges it. Reads and file edits are allowed inside the worktree only, so a secret read or
    an outside write still waits; every other tool goes to the trio. Nothing here becomes an owner ask."""
    tool, given, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd")
    if not isinstance(given, dict):
        return "trio", "the permission request has no readable input"
    if tool == "Bash":
        # Missing and JSON null are the same: an ask stored before #893 has no hooks keys and stays an allow.
        return judgement(given.get("command"), protected_branches, cwd, event.get("hooks_path"), event.get("hooks_root"))
    if tool in READ_TOOLS | WRITE_TOOLS:
        path = given.get("file_path") or given.get("notebook_path") or given.get("path") or ("." if tool in READ_TOOLS else "")
        if isinstance(path, str) and path and inside(path, cwd) and not (tool in WRITE_TOOLS and _in_git_dir(path)):
            return "allow", ""
        return "trio", f"{tool} of {path or 'an unnamed path'} is outside the worktree"
    return "trio", f"{tool or 'this tool'} has no permission rule"


def denial_reason(command: str, protected_branches: tuple[str, ...]) -> str | None:
    try:
        return OWNED if _denied(command, protected_branches, 0) else None
    except ImplicitPush:
        return "Push with git first and pass --head (or -H) with an explicit branch to gh pr create. " + OWNED
    except Opaque:
        return UNPARSED
    except (ValueError, TypeError, RecursionError):
        return UNPARSED if (not isinstance(command, str) or GUARDED_TEXT.search(command)
                            or _inline_interpreter_text(command)) else None
