#!/usr/bin/env python3
"""The tracker's File ownership rows, read one way by every script that needs them.

A row names a context and the paths it owns, written as a table row `| context | paths |` or as a
bullet `- context: paths`. Audit read both and dispatch read only tables, so a row the audit
accepted was refused at launch (#74).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from md import cells as _cells, is_separator as _is_separator, section as _section

HEADING = "## File ownership"
_NONE = re.compile(r"none(?![\w./-])", re.I)


@dataclass(frozen=True)
class Row:
    context: str
    paths: str


def parse(text: str) -> list[Row]:
    """Every row in the section's body, in order; the table header and separator are not rows."""
    rows: list[Row] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) and ":" in stripped:
            context, _, paths = stripped[2:].partition(":")
            rows.append(Row(context.strip(), paths.strip()))
            continue
        if not stripped.startswith("|"):
            continue
        cells = _cells(line)
        if _is_separator(cells) or len(cells) < 2 or cells[0].lower() == "context":
            continue
        rows.append(Row(cells[0], cells[1]))
    return rows


def cell(text: str, keys: set[str]) -> str | None:
    """The paths of the first File ownership row in the tracker `text` keyed on one of `keys`, or None."""
    return next((row.paths for row in parse("\n".join(_section(text, HEADING))) if row.context in keys), None)


def issue_scopes(body: str) -> list[tuple[str, str]] | None:
    """Issue `## File ownership`: `| Mode | Paths |` or `- write: paths` / `- read-only: paths`.

    Reuse the tracker row reader. Missing, empty or malformed declarations have no scope (None).
    Only an explicit `.` path declares the whole repository.
    Paths are literal repository-relative paths, just as for the tracker's overlap rule.
    """
    scopes: dict[str, str] = {}
    lines = _section(body, HEADING)
    for line in lines:
        if not line.strip():
            continue
        if line.strip().startswith("|"):
            cells = _cells(line)
            if len(cells) != 2:
                return None
            if _is_separator(cells) or [c.lower() for c in cells] in (["mode", "paths"], ["context", "paths"]):
                continue
        if not parse(line):
            return None
    for row in parse("\n".join(lines)):
        mode = row.context.strip().lower()
        if mode == "mode" and row.paths.lower() == "paths":
            continue
        if mode not in ("write", "read", "read-only"):
            return None
        if not row.paths:
            continue
        tokens = re.findall(r"`([^`]+)`", row.paths) or row.paths.split(",")
        if any(token.strip() != "." and _paths(token) == ["."] for token in tokens):
            return None
        if "`" in row.paths and not re.fullmatch(r"\s*`[^`]+`(?:\s*,\s*`[^`]+`)*\s*", row.paths):
            return None
        if any(token != token.strip() for token in re.findall(r"`([^`]+)`", row.paths)):
            return None
        paths = _paths(row.paths)
        if not paths:
            return None
        for path in paths:
            if (path.startswith("/") or (path != "." and any(part in ("", ".", "..") for part in path.split("/")))
                    or any(c in path for c in "*?[]`") or _paths(path) != [path]):
                return None
            # A duplicate write cannot be narrowed by a later read row.
            scopes[path] = "write" if mode == "write" or scopes.get(path) == "write" else "read"
    return list(scopes.items()) or None


def read_only(paths: str) -> bool:
    """A cell that starts with the word `none`, not the start of a path, owns nothing, so it changes nothing (#57)."""
    return bool(_NONE.match(paths.strip()))


def _paths(cell: str) -> list[str]:
    """The paths a cell owns: its backticked tokens, or its comma-separated words when it has none; the launcher's
    `; worktree ...` suffix is no part of it, and a read-only cell owns none. Segment-wise: no `./`, no trailing `/`; `.` is the whole repository."""
    cell = cell.split("; worktree", 1)[0]
    if read_only(cell):
        return []
    tokens = re.findall(r"`([^`]+)`", cell) or cell.split(",")
    return [p for p in (t.strip().rstrip("/").removeprefix("./") for t in tokens) if p]


def overlaps(a: str, b: str) -> bool:
    """One path of `a` equals, contains or is contained in one of `b` (`src/a/` and `src/a/x.py` do; `src/a` and `src/ab` do not); `.` contains every path.
    ponytail: a glob character is read as part of the name, so `src/*.py` overlaps only a path spelled that way; match globs when a tracker writes them."""
    return any("." in (x, y) or x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in _paths(a) for y in _paths(b))


# #915 (§8): the plan stage's own files, one per issue, so plan runs never serialize on each other.
PLAN_DIR = "docs/plans"
# #1006: UTF-8 byte cap on Decision.context for topic `scope` within the trio's prompt budget; tune from real prompts.
SCOPE_CONTEXT_CAP = 12_000
_PLAN_TRUNCATED = "\n… [plan truncated]"
_BODY_TRUNCATED = "\n… [issue body truncated]"
_TITLE_TRUNCATED = " … [title truncated]"


def plan_path(issue_key: str) -> str:
    """The one file an issue's plan run writes."""
    return f"{PLAN_DIR}/issue-{issue_key.rpartition('#')[2]}.md"


def _truncate_utf8(text: str, budget: int, suffix: str) -> str:
    if budget <= 0:
        return suffix.lstrip()
    head = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return (head + suffix) if head else suffix.lstrip()


def scope_consult_context(*, title: str, body: str, plan_text: str,
                          scopes: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> str:
    """#1006: bounded context for a `scope` trio consult — proposed paths first, then issue title, body, and plan."""
    listed = "; ".join(f"{mode}: {path}" for path, mode in scopes) or "(none)"
    title_part = title.strip() if isinstance(title, str) and title.strip() else "(empty)"
    body_part = body.strip() if isinstance(body, str) and body.strip() else "(empty)"
    has_plan = isinstance(plan_text, str) and bool(plan_text.strip())
    plan_part = plan_text.strip() if has_plan else "(not found in worktree)"

    def render(t: str, b: str, p: str) -> str:
        plan_block = f"Plan:\n{p}" if has_plan else f"Plan: {p}"
        return "\n\n".join((
            f"Proposed file ownership: {listed}",
            f"Issue title: {t}",
            f"Issue body:\n{b}",
            plan_block,
        ))

    out = render(title_part, body_part, plan_part)
    if len(out.encode("utf-8")) <= SCOPE_CONTEXT_CAP:
        return out
    if has_plan:
        overhead = len(render(title_part, body_part, "").encode("utf-8")) + len(_PLAN_TRUNCATED.encode("utf-8"))
        plan_part = _truncate_utf8(plan_part, SCOPE_CONTEXT_CAP - overhead, _PLAN_TRUNCATED)
        out = render(title_part, body_part, plan_part)
        if len(out.encode("utf-8")) <= SCOPE_CONTEXT_CAP:
            return out
    overhead = len(render(title_part, "", plan_part).encode("utf-8")) + len(_BODY_TRUNCATED.encode("utf-8"))
    body_part = _truncate_utf8(body_part, SCOPE_CONTEXT_CAP - overhead, _BODY_TRUNCATED)
    out = render(title_part, body_part, plan_part)
    if len(out.encode("utf-8")) <= SCOPE_CONTEXT_CAP:
        return out
    overhead = len(render("", body_part, plan_part).encode("utf-8")) + len(_TITLE_TRUNCATED.encode("utf-8"))
    title_part = _truncate_utf8(title_part, SCOPE_CONTEXT_CAP - overhead, _TITLE_TRUNCATED)
    return render(title_part, body_part, plan_part)


def proposal_scopes(entries) -> list[tuple[str, str]] | None:
    """A plan's `file_ownership` proposal (#915), checked by the same parser as an issue's section. None when malformed:
    no entries, a mode other than write or read, or any path the parser refuses. `.` is never proposed."""
    try:
        rows = [(str(entry["path"]).strip(), str(entry["mode"]).strip()) for entry in entries]
    except (TypeError, KeyError, AttributeError):
        return None
    if not rows or any(mode not in ("write", "read") for _, mode in rows) or any(path in ("", ".") for path, _ in rows):
        return None
    parsed = issue_scopes(scope_section(rows))
    return parsed if parsed and not any(path == "." for path, _ in parsed) else None


def scope_section(scopes: list[tuple[str, str]]) -> str:
    """Canonical issue syntax, checked by intake before the migration proposes it."""
    return HEADING + '\n' + '\n'.join(f'- {mode}: `{path}`' for path, mode in scopes) + '\n'


def section_problem(body: str) -> str:
    """A migration cannot safely replace duplicate headings or a quoted example."""
    headings, fenced = 0, False
    for line in body.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        if line.strip() == HEADING:
            headings += 1
            if fenced:
                return 'File ownership heading is inside a code example; the owner must clarify'
    return 'multiple File ownership headings; the owner must clarify' if headings > 1 else ''


def with_issue_scopes(body: str, scopes: list[tuple[str, str]]) -> str:
    """Replace just the ownership section, keeping the rest of the issue verbatim."""
    if section_problem(body):
        raise ValueError(section_problem(body))
    lines = body.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if line.strip() == HEADING:
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
            return ''.join(lines[:start]) + scope_section(scopes) + ('\n' if end < len(lines) else '') + ''.join(lines[end:])
    return body.rstrip('\n') + ('\n\n' if body else '') + scope_section(scopes)


def _aliases(sources: dict[str, str], symbols: set[str]) -> dict[str, set[str]]:
    """Every name a file or a referenced top-level Python definition can be cited by, to the paths that carry it."""
    aliases: dict[str, set[str]] = {}
    for path, code in sources.items():
        aliases.setdefault(PurePosixPath(path).name, set()).add(path)
        if path.endswith('.py'):
            aliases.setdefault(PurePosixPath(path).stem, set()).add(path)
            if not symbols or not any(re.search(r'\b(?:def|class)\s+' + re.escape(symbol) + r'\b', code) for symbol in symbols):
                continue
            try:
                tree = ast.parse(code)
            except (SyntaxError, ValueError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in symbols:
                    aliases.setdefault(node.name, set()).add(path)
    return aliases


def _is_read_task(title: str, body: str, text: str) -> bool:
    return bool(
        re.match(r'(?i)^\s*read-only (?:audit|review|task)\b', title)
        or (re.match(r'(?i)^\s*(?:audit|review|inspect)\b', title)
            and re.search(r'(?i)\bread-only\b', text))
        or re.search(r'(?im)^\s*(?:this task|this assignment|scope)\s*(?:is|:)\s*read-only\b', body)
    )


def _named(line: str, files: set[str], aliases: dict[str, set[str]]) -> list[str]:
    """The files and symbols a line cites. Bare symbols and explicit directory references must be code spans."""
    tokens = set(re.findall(r'(?<![\w/])(?:[\w.-]+/)*[\w.-]+\.(?:py|md|json|toml|ya?ml|js|ts|css|html)\b', line))
    for token in re.findall(r'`([^`]+)`', line):
        token = re.sub(r':\d+(?:-\d+)?$', '', token).removesuffix('()')
        if token in aliases or token in files or (token.endswith('/') and any(p.startswith(token) for p in files)):
            tokens.add(token)
    return sorted(tokens)


def _line_mode(line: str, read_task: bool) -> tuple[str, bool]:
    """(mode, forbidden): a reference line or a prohibition reads, anything else in a write task writes."""
    reference = bool(re.match(r'(?i)^\s*(?:[-*]\s*)?(?:plan:|ledger:|reference:|see[ :]|read[ :])', line))
    reference = reference or bool(re.search(r'(?i)\b(?:like|for example)\s+`', line))
    forbidden = bool(re.search(r"(?i)\b(?:do not|don't|must not|never) (?:edit|change|modify|write|touch)\b", line))
    return ('read' if read_task or reference or forbidden else 'write'), forbidden


def _add_companion_tests(scopes: dict[str, str], files: set[str], prohibited: set[str]) -> None:
    """A written Python module also writes its existing test file, unless the issue forbids that path."""
    for path, mode in list(scopes.items()):
        if mode != 'write' or not path.endswith('.py') or path.startswith('evals/'):
            continue
        for test in (f'evals/test_{PurePosixPath(path).stem}.py', f'tests/test_{PurePosixPath(path).stem}.py'):
            if test in files and not any(overlaps(test, denied) for denied in prohibited):
                scopes[test] = 'write'


def derive_scopes(title: str, body: str, sources: dict[str, str]) -> tuple[list[tuple[str, str]], str]:
    """Conservative backfill from named, tracked code, never a repository fallback.

    The driver supplies the repository's code. Unique basenames and referenced Python
    definitions resolve to their actual files; companion tests come from that same tree.
    Examples and plan citations are evidence to read, not implementation targets.
    Anything ambiguous is returned for the owner, without granting a partial scope.
    """
    text = title + '\n' + body
    text = re.sub(r'(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$', '', text)
    # A broken declaration is not evidence for its own replacement.
    text = re.sub(r'(?ms)^## File ownership\s*\n.*?(?=^## |\Z)', '', text)
    read_task = _is_read_task(title, body, text)
    files = set(sources)
    symbols = {token.removesuffix('()') for token in re.findall(r'`([A-Za-z_]\w*\(\))`', text)}
    symbols -= {PurePosixPath(p).stem for p in files} | {PurePosixPath(p).name for p in files}
    aliases = _aliases(sources, symbols)
    scopes: dict[str, str] = {}
    problems: set[str] = set()
    prohibited: set[str] = set()
    for line in text.splitlines():
        mode, forbidden = _line_mode(line, read_task)
        for token in _named(line, files, aliases):
            if token in files:
                matches = {token}
            elif token.endswith('/'):
                matches = {token.rstrip('/')}
            else:
                matches = aliases.get(token, set())
            if len(matches) != 1:
                problems.add('ambiguous or missing named file/symbol: ' + token)
                continue
            path = next(iter(matches))
            # '.' is possible only in an existing, explicit valid declaration.
            if path == '.' or any(part in ('', '.', '..') for part in path.split('/')):
                problems.add('noncanonical named path')
                continue
            if forbidden:
                prohibited.add(path)
            path_mode = mode
            if path.endswith('.md') and PurePosixPath(path).name not in title and not re.search(r'(?i)\b(?:edit|update|rewrite|change|write|remove|retire|close)\b', line):
                path_mode = 'read'
            scopes[path] = 'write' if path_mode == 'write' or scopes.get(path) == 'write' else 'read'
    for path in scopes:
        if any(overlaps(path, denied) for denied in prohibited):
            scopes[path] = 'read'
    if problems:
        return [], '; '.join(sorted(problems))
    if not scopes or (not read_task and all(mode == 'read' for mode in scopes.values())):
        return [], 'no unambiguous implementation paths; the owner must supply the scope'
    if not read_task:
        _add_companion_tests(scopes, files, prohibited)
    return sorted(scopes.items()), ''
