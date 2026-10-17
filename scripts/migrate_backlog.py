#!/usr/bin/env python3
"""Propose a disposition for every row in a tracker's `## Issues`, then create only what the user approves.

Also backfills issue file ownership with --file-ownership; see docs/backlog.md.

The triage is the expensive half; the moving is one API call per issue. Existing rows may be stale,
so this script proposes a reasoned disposition and waits for a human to edit the report before apply.

    python3 migrate_backlog.py --tracker <tracker.md> --report backlog-migration.md
    # …the user edits the disposition column…
    python3 migrate_backlog.py --tracker <tracker.md> --apply backlog-migration.md --config CLAUDE.md --commit

Each row is keyed by a hash of its own text. That makes the report and the apply step agree about
*which row* even as the tracker moves underneath them, and it makes a row that changed after the
report **unmigratable rather than silently migrated as something else** — no disposition is not
consent.

The table is read through `md`'s splitter, which is mark-aware: a `|` inside a code span
is a pipe, not a cell edge (finding 100). A second splitter here would reintroduce that bug.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import base64
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog as bl
import ownership
from md import (cells as _cells, is_separator as _is_separator, section as _section, split_row as _split_row,
                unmark as _unmark)
from tracker import TASK_COLS, TASK_HEADERS, anchor as _anchor, clip_name, short_name

MOVE, KEEP, CLOSED, CHECK = "move", "keep", "closed", "check"
DISPOSITIONS = (MOVE, KEEP, CLOSED, CHECK)

# A row that SHOUTS its own completion. Case-sensitive and deliberately so: sessions close their
# own work in capitals — `**FINISHED 00:12**`, `CLEARED 01:05`, `DELIVERED 00:02` — while the same
# words in ordinary prose ("the fix landed", "resolved by") say nothing about this row's state.
# Matching case-insensitively flagged a row whose only crime was the word "resolved" in a sentence.
DONE_WORDS = re.compile(r"\b(?:DONE|FINISHED|LANDED|CLEARED|CANCELLED|SUPERSEDED|RESOLVED|DELIVERED)\b")
OPEN_STATES = ("open", "waiting", "orphaned")
UNOWNED = ("", "-", "—", "unassigned", "none", "nobody")
TITLE_CAP = 90


@dataclass(frozen=True)
class Row:
    """One `## Issues` row, keyed by its own text."""

    item: str
    owner: str
    state: str
    since: str
    size: str

    @property
    def key(self) -> str:
        return hashlib.sha256(self.item.encode()).hexdigest()[:8]

    @property
    def kind(self) -> str:
        return (self.state.split() or ["open"])[0].lower()

    @property
    def title(self) -> str:
        """`short_name` ends at a clause boundary and loses no letters; `clip_name` is the hard cut
        that respects a mark pair. board-ux split them apart for exactly this pairing."""
        return clip_name(short_name(_unmark(self.item)), TITLE_CAP).strip()

    @property
    def unowned(self) -> bool:
        return _unmark(self.owner).strip().lower() in UNOWNED


@dataclass(frozen=True)
class Proposal:
    row: Row
    disposition: str
    why: str


@dataclass(frozen=True)
class Outcome:
    created: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def issue_rows(path: Path) -> tuple[Row, ...]:
    """Every row of `## Issues`, read the way the board reads `## Tasks`."""
    lines = [l for l in _section(Path(path).read_text(), "## Issues") if l.strip().startswith("|")]
    if not lines:
        return ()
    header = [c.lower() for c in _cells(lines[0])]
    cols = next((h for h in TASK_HEADERS if header == list(h)), TASK_COLS)
    out: list[Row] = []
    for line in lines[1:]:
        raw = _split_row(line)
        cells = [c.strip() for c in raw]
        if _is_separator(cells) or not any(cells):
            continue
        if len(cells) > len(cols):
            fixed = _anchor(raw, cols)
            cells = [c.strip() for c in fixed] if fixed else cells
        fields = dict(zip(cols, (cells + [""] * len(cols))[:len(cols)]))
        out.append(Row(item=fields.get("item", ""), owner=fields.get("owner", ""),
                       state=fields.get("state", ""), since=fields.get("since", ""),
                       size=fields.get("size", "")))
    return tuple(out)


def fingerprint(path: Path) -> str:
    """Twelve hex of the tracker's text. The report says which tracker it read, so an apply step can
    say "this has moved" instead of silently working from a stale triage."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


FINGERPRINT = re.compile(r"at `([0-9a-f]{12})`")


def read_fingerprint(text: str) -> str:
    m = FINGERPRINT.search(text)
    return m.group(1) if m else ""


def propose(row: Row) -> Proposal:
    """A proposal and the reason for it. Never a decision — the user can correct it."""
    shouts = bool(DONE_WORDS.search(row.item))
    if row.kind == "done":
        return Proposal(row, CLOSED, "state says done; it closed here and does not need a GitLab row")
    if row.kind in OPEN_STATES and shouts:
        return Proposal(row, CHECK, "the state cell says open but the prose claims it is finished — "
                                    "the row disputes itself and a human settles it")
    if row.kind == "running":
        return Proposal(row, KEEP, "running, so it is being worked and belongs in the tracker")
    if not row.unowned:
        return Proposal(row, KEEP, f"owned by {_unmark(row.owner).strip()}, so it is being worked")
    return Proposal(row, MOVE, "open and unowned, which is what backlog means")


def proposals(rows: tuple[Row, ...]) -> tuple[Proposal, ...]:
    return tuple(propose(r) for r in rows)


def report(items: tuple[Proposal, ...], source: str = "", fingerprint: str = "") -> str:
    """The file the user edits. Every row proposes one word and says why."""
    counts = {d: sum(1 for p in items if p.disposition == d) for d in DISPOSITIONS}
    head = [
        "# Backlog migration — proposed dispositions",
        "",
        (f"{len(items)} rows read from tracker `{source}` at `{fingerprint}` `## Issues`."
         if source else f"{len(items)} rows."),
        "",
        "**Nothing has been created.** Edit the `disposition` column and nothing else, then run the apply step.",
        "The key is a hash of the row's own text: if a row changes after this file is written, its key changes "
        "and it is skipped rather than migrated as something else.",
        "",
        "| disposition | what it means |",
        "|---|---|",
        f"| `{MOVE}` | create it as a GitLab issue and drop the tracker row |",
        f"| `{KEEP}` | it is being worked; it stays in the tracker |",
        f"| `{CLOSED}` | it is already finished; no GitLab row, the tracker keeps the record |",
        f"| `{CHECK}` | the row disputes itself; decide before it moves |",
        "",
        "Proposed: " + ", ".join(f"{counts[d]} {d}" for d in DISPOSITIONS) + ".",
        "",
        "| key | disposition | state | owner | size | title | why this was proposed |",
        "|---|---|---|---|---|---|---|",
    ]
    body = [
        f"| `{p.row.key}` | {p.disposition} | {p.row.state or '—'} | {_unmark(p.row.owner).strip() or '—'} "
        f"| {p.row.size or '—'} | {p.row.title.replace('|', '\\|')} | {p.why} |"
        for p in items
    ]
    return "\n".join(head + body) + "\n"


ROW = re.compile(r"^\|\s*`([0-9a-f]{8})`\s*\|\s*([A-Za-z]+)\s*\|")


def read_report(text: str) -> dict[str, str]:
    """The dispositions a human left in the file, keyed by row. Anything unrecognised is left out."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = ROW.match(line.strip())
        if m and m.group(2).lower() in DISPOSITIONS:
            out[m.group(1)] = m.group(2).lower()
    return out


def body_for(row: Row, source: str = "") -> str:
    where = f"\n\nMigrated from `{source}` `## Issues` on 2026-09-20." if source else ""
    return (f"{row.item}\n\n---\n\n- owner in the tracker: {_unmark(row.owner).strip() or 'unassigned'}\n"
            f"- state in the tracker: {row.state or 'open'}\n- since: {row.since or 'unrecorded'}\n"
            f"- size: {row.size or 'unsized'}{where}")


def migrate(rows: tuple[Row, ...], decided: dict[str, str], make, source: str = "",
            labels: tuple[str, ...] = ()) -> Outcome:
    """Create only the rows a human marked `move`. No disposition is not consent."""
    created, skipped, unknown, failed = [], [], [], []
    for row in rows:
        want = decided.get(row.key)
        if want is None:
            unknown.append(row.title)
            continue
        if want != MOVE:
            skipped.append(row.title)
            continue
        try:
            make(row.title, body_for(row, source), labels)
        except Exception as exc:
            failed.append(f"{row.title}: {type(exc).__name__}: {exc}")
            continue
        created.append(row.title)
    return Outcome(tuple(created), tuple(skipped), tuple(unknown), tuple(failed))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--file-ownership" in argv:
        return ownership_main([a for a in argv if a != "--file-ownership"])
    ap = argparse.ArgumentParser(description="Propose, then apply, a backlog migration into GitLab.")
    ap.add_argument("--tracker", required=True, help="the day's tracker holding `## Issues`")
    ap.add_argument("--report", metavar="FILE", help="write the proposal file and stop")
    ap.add_argument("--apply", metavar="FILE", help="read an edited proposal file and create the moves")
    ap.add_argument("--config", default="CLAUDE.md", help="workspace CLAUDE.md holding the `- Backlog:` line")
    ap.add_argument("--labels", default="", help="comma-separated labels for every created issue")
    ap.add_argument("--commit", action="store_true", help="actually create. Without it nothing is written.")
    args = ap.parse_args(argv)

    tracker = Path(args.tracker)
    if not tracker.is_file():
        print(f"no tracker at {tracker}", file=sys.stderr)
        return 2
    rows = issue_rows(tracker)
    if not rows:
        print(f"no `## Issues` rows in {tracker}", file=sys.stderr)
        return 2

    if args.report:
        Path(args.report).write_text(report(proposals(rows), source=str(tracker),
                                            fingerprint=fingerprint(tracker)))
        print(f"{len(rows)} rows proposed in {args.report}; nothing created")
        return 0

    if not args.apply:
        for p in proposals(rows):
            print(f"{p.row.key} {p.disposition:7} {p.row.title}")
        return 0

    edited = Path(args.apply).read_text()
    was = read_fingerprint(edited)
    now = fingerprint(tracker)
    if was and was != now:
        print(f"the tracker has changed since this report was written ({was} → {now}); rows that moved "
              f"will have no disposition and will not be created", file=sys.stderr)
    decided = read_report(edited)
    cfg = bl.backlog_from_config(Path(args.config))
    if cfg is None:
        print("no `- Backlog:` line in the config", file=sys.stderr)
        return 2
    labels = tuple(p.strip() for p in args.labels.split(",") if p.strip())
    made: list[bl.Written] = []

    def make(title: str, body: str, want: tuple[str, ...]) -> None:
        written = bl.create(cfg, title, body=body, labels=want, commit=args.commit)
        made.append(written)
        if written.error:
            raise RuntimeError(written.error)

    got = migrate(rows, decided, make, source=str(tracker), labels=labels)
    for one in made:
        print(bl.write_line(one))
    print(f"created {len(got.created)}, kept {len(got.skipped)}, "
          f"no disposition {len(got.unknown)}, failed {len(got.failed)}")
    for name in got.unknown:
        print(f"no disposition, so not created: {name}", file=sys.stderr)
    for why in got.failed:
        print(f"failed: {why}", file=sys.stderr)
    if not args.commit:
        print("nothing was written: add --commit to make these real", file=sys.stderr)
    return 0 if not got.failed else 1



def ownership_gh(args: list[str], body: str | None = None):
    """gh is the only issue writer. Body text goes through a file, never shell quoting."""
    with tempfile.TemporaryDirectory() as directory:
        if body is not None:
            path = Path(directory) / 'body.md'
            path.write_text(body, encoding='utf-8')
            args = [str(path) if arg == '-' else arg for arg in args]
        done = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=90)
    if done.returncode:
        raise ValueError(f'gh {args[0]} failed: {done.stderr.strip()}')
    return json.loads(done.stdout) if done.stdout.strip().startswith(('{', '[')) else {}


def ownership_sources(root: Path) -> dict[str, str]:
    """Read tracked text from the chosen workspace, excluding fixture/example trees."""
    done = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True)
    sources = {}
    for name in done.stdout.decode().split('\0'):
        if not name or any(part in ('fixtures', 'fixture', 'cases', '_vendor', 'node_modules') for part in Path(name).parts):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            sources[name] = path.read_text(encoding='utf-8')
        except (UnicodeError, OSError):
            continue
    return sources


def ownership_validator(root: Path):
    """Load the actual intake parser in an isolated Python process, not a copied grammar.

    --parser-root lets this migration be reviewed before #820 lands. After it lands,
    the default is this release's scripts/ directory. A legacy fallback is rejected.
    """
    scripts = root / 'scripts'
    program = ('import sys,json; sys.path.insert(0,sys.argv[1]); import issue_gate; '
               'print(json.dumps([issue_gate.ownership.issue_scopes(b) for b in json.load(sys.stdin)]))')

    def validate(bodies):
        done = subprocess.run([sys.executable, '-c', program, str(scripts)], input=json.dumps(bodies),
                              text=True, capture_output=True, timeout=30)
        if done.returncode:
            raise ValueError('cannot load issue_gate parser: ' + done.stderr.strip())
        return json.loads(done.stdout)
    return validate


def ownership_digest(title: str, body: str) -> str:
    return hashlib.sha256(json.dumps([title, body], ensure_ascii=True).encode()).hexdigest()


def ownership_plan(repo: str, issues: list[dict], sources: dict[str, str], validate) -> dict:
    if validate([''])[0] is not None:
        raise ValueError('requires #820 issue_gate parser (missing sections must return None); use --parser-root')
    rows = []
    for issue in sorted(issues, key=lambda row: row['number']):
        title, body = issue['title'], issue.get('body') or ''
        row = dict(number=issue['number'], original=ownership_digest(title, body), section='', scopes=[], status='unresolved', reason='')
        problem = ownership.section_problem(body)
        if problem:
            row['reason'] = problem
        elif validate([body])[0] is not None:
            row.update(status='existing', reason='valid declaration retained')
        else:
            scopes, why = ownership.derive_scopes(title, body, sources)
            if scopes:
                candidate = ownership.with_issue_scopes(body, scopes)
                parsed = validate([candidate])[0]
                if parsed is None or sorted(map(tuple, parsed)) != sorted(scopes):
                    row['reason'] = 'issue_gate parser rejected the proposed section'
                else:
                    row.update(status='derived', scopes=scopes, section=ownership.scope_section(scopes),
                               proposed=ownership_digest(title, candidate), reason='named tracked files/symbols and their existing companion tests')
            else:
                row['reason'] = why
        rows.append(row)
    code_hash = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    return dict(repo=repo, code_sha256=code_hash, issues=rows)


def ownership_report(workspaces: list[dict]) -> str:
    lines = ['# File ownership backfill: dry-run', '',
             'No issue body was written. Unresolved issues need the owner to supply their scope.',
             'Only explicitly declared `.` scopes cover a whole repository. Existing valid declarations are retained.',
             'Derived scopes use named tracked files or unique Python definitions and existing companion tests.',
             'Plan citations alone do not establish implementation scope. All proposed sections passed the #820 issue_gate parser.', '']
    for workspace in workspaces:
        rows = workspace['issues']
        counts = ', '.join(f"{sum(r['status'] == s for r in rows)} {s}" for s in ('derived', 'existing', 'unresolved'))
        lines += [f"## {workspace['repo']}", '', f'{len(rows)} open issues: {counts}.', '',
                  f"Code snapshot SHA256: `{workspace['code_sha256']}`", '', '### Needs the owner', '']
        for row in rows:
            if row['status'] == 'unresolved':
                lines.append(f"- [#{row['number']}](https://github.com/{workspace['repo']}/issues/{row['number']}): {row['reason']}")
        lines += ['', '### Proposed or retained declarations', '']
        for row in rows:
            if row['status'] == 'unresolved':
                continue
            lines += [f"#### [#{row['number']}](https://github.com/{workspace['repo']}/issues/{row['number']}) — {row['status']}", '',
                      f"Original title/body SHA256: `{row['original']}`", '']
            if row['section']:
                lines += ['```markdown', row['section'].rstrip(), '```', '']
            else:
                lines += ['Existing valid declaration retained without changes.', '']
    return '\n'.join(lines).rstrip() + '\n'


def apply_ownership(plan: dict, plan_text: str, approval_pr: str, plan_path: str, gh, validate) -> list[dict]:
    """Apply exactly the plan in the approved PR head, skipping closed or changed issues."""
    if plan.get('version') != 1:
        raise ValueError('unsupported ownership plan version')
    if validate([''])[0] is not None:
        raise ValueError('requires #820 issue_gate parser')
    match = re.fullmatch(r'https://github.com/([^/]+/[^/]+)/pull/(\d+)', approval_pr)
    if not match or not plan_path or plan_path.startswith('/') or '..' in Path(plan_path).parts:
        raise ValueError('use a GitHub approval PR URL and repository-relative --plan-path')
    approved_repo = match.group(1)

    def approved():
        pr = gh(['pr', 'view', approval_pr, '--json', 'headRefOid,comments,url'])
        sha, verdict = pr['headRefOid'], ''
        for comment in pr['comments']:
            for found in re.finditer(r'(?m)^Architect: (APPROVED|CHANGES) ([0-9a-f]{40})[ \t]*$', comment['body']):
                if found.group(2) == sha:
                    verdict = found.group(1)
        if verdict != 'APPROVED':
            raise ValueError('architect approval is required for the PR current head')
        return sha

    head = approved()
    reviewed = gh(['api', f'repos/{approved_repo}/contents/{plan_path}?ref={head}'])
    if base64.b64decode(reviewed['content']).decode() != plan_text:
        raise ValueError('local plan differs from the reviewed plan at the approved head')
    results = []
    for workspace in plan['workspaces']:
        repo = workspace['repo']
        for row in workspace['issues']:
            if row['status'] != 'derived':
                continue
            result = dict(repo=repo, number=row['number'], status='skipped', reason='')
            try:
                now = gh(['issue', 'view', str(row['number']), '--repo', repo, '--json', 'title,body,state'])
                body, title = now.get('body') or '', now['title']
                digest = ownership_digest(title, body)
                if now['state'].upper() != 'OPEN':
                    result['reason'] = 'issue is closed'
                elif digest == row['proposed']:
                    result['status'] = 'already applied'
                elif digest != row['original']:
                    result['reason'] = 'issue changed since dry-run; regenerate the plan'
                else:
                    scopes = [tuple(s) for s in row['scopes']]
                    candidate = ownership.with_issue_scopes(body, scopes)
                    parsed = validate([candidate])[0]
                    if parsed is None or sorted(map(tuple, parsed)) != sorted(scopes) or ownership.scope_section(scopes) != row['section']:
                        raise ValueError('issue_gate rejected the saved proposal')
                    if ownership_digest(title, candidate) != row['proposed']:
                        raise ValueError('saved proposal digest does not match')
                    if approved() != head:
                        raise ValueError('approval PR head changed during apply')
                    gh(['issue', 'edit', str(row['number']), '--repo', repo, '--body-file', '-'], candidate)
                    result['status'] = 'written'
            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                result.update(status='failed', reason=str(exc))
            results.append(result)
    return results


def ownership_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description='Backfill open GitHub issues with reviewed file ownership sections.')
    ap.add_argument('--workspace', action='append', default=[], metavar='OWNER/REPO=PATH', help='repeat for every engine workspace')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', metavar='PLAN.json')
    ap.add_argument('--report', required=True, help='dry-run Markdown report or apply JSON results')
    ap.add_argument('--parser-root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--approval-pr', help='GitHub PR URL carrying Architect: APPROVED <current-head>')
    ap.add_argument('--plan-path', help='path of the reviewed plan in the approval PR repository')
    args = ap.parse_args(argv)
    try:
        validate = ownership_validator(args.parser_root)
        if args.apply:
            if not args.approval_pr or not args.plan_path:
                ap.error('--apply requires --approval-pr and --plan-path')
            plan_text = Path(args.apply).read_text(encoding='utf-8')
            results = apply_ownership(json.loads(plan_text), plan_text, args.approval_pr, args.plan_path, ownership_gh, validate)
            Path(args.report).write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
            print(f"{sum(r['status'] == 'written' for r in results)} written; see {args.report}")
            return int(any(r['status'] in ('failed', 'skipped') for r in results))
        if not args.workspace:
            ap.error('--dry-run requires at least one --workspace OWNER/REPO=PATH; repeat for every engine workspace')
        workspaces, seen = [], set()
        for spec in args.workspace:
            repo, sep, path = spec.partition('=')
            if not sep or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or repo in seen:
                ap.error('invalid or duplicate --workspace; expected OWNER/REPO=PATH')
            seen.add(repo)
            raw = ownership_gh(['api', f'repos/{repo}/issues?state=open&per_page=100', '--paginate', '--slurp'])
            issues = [i for page in raw for i in page] if raw and isinstance(raw[0], list) else raw
            issues = [i for i in issues if 'pull_request' not in i]
            workspaces.append(ownership_plan(repo, issues, ownership_sources(Path(path)), validate))
        scripts = args.parser_root / 'scripts'
        parser_hashes = {name: hashlib.sha256((scripts / name).read_bytes()).hexdigest()
                         for name in ('issue_gate.py', 'ownership.py', 'md.py')}
        plan = dict(version=1, parser_sha256=parser_hashes, workspaces=workspaces)
        report = Path(args.report)
        report.write_text(ownership_report(workspaces), encoding='utf-8')
        report.with_suffix('.json').write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
        print(f'Dry-run only: {sum(len(w["issues"]) for w in workspaces)} issues; see {report} and {report.with_suffix(".json")}')
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
