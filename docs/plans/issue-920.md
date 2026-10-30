# Issue #920: Format Worker Reports in Task History Without Pasting Raw Markdown Inline

## Context & Problem Statement

On the store-backed task page (`scripts/issue_page.py` on `origin/develop`), the **How it got here** section renders every `db.log(item.issue_key)` entry directly into a single `.step` row with HTML escaping only:

```python
steps_list.extend((parse_stamp(entry.at, now), escape(entry.message))
                  for entry in db.log(item.issue_key))
steps = "\n".join(f'<div class="step"><span class="when">{_clock(at, now)}</span>'
                  f'<span class="dot"></span><span>{text}</span></div>'
                  for at, text in sorted(steps_list))
```

When a worker session ends, `engine._close_segment` (`scripts/engine.py:645`) and `worker.collect` (`scripts/worker.py:578`) append a `LogEntry` to `store.db`:

```python
f"worker {stage} attempt {attempt}: {outcome}" + (f": {reason}" if reason else "")
```

Two paths embed a worker's entire multi-line Markdown `report.summary` inside `reason`:

1. **Suite-not-green `human_review` (`scripts/reconcile.py:59-63`, `scripts/worker.py:545`, `scripts/reconcile.py:373`)**:
   `reconcile.suite_reason(counts, summary)` builds:
   ```python
   f"suite is not green; Suite: {shown}; worker said: {summary}"
   ```
   When a worker calls `report(outcome="done", summary=<6,300-char Markdown plan>)` without green suite counts, `entry.message` becomes a 6,434-character, 69-line string (`drago #2`, step `18:49`).
2. **Non-`done` worker reports (`blocked`, `failed`, or `human_review`) in `worker.collect` (`scripts/worker.py:551-552`)**:
   `worker.collect` sets `outcome, reason = reports[-1].outcome, reports[-1].summary` (and `engine._finish_worker` at `scripts/engine.py:660` appends `; {verdict.reason}` if `verdict.human_review` is also set). When a worker calls `report(outcome="blocked", summary=<2,750-char Markdown analysis>)`, `entry.message` becomes a 2,795-character, 21-line string (`chief-of-stuff #688`, step `13:37`).

Because `issue_page.py` wraps `escape(entry.message)` in a plain `<span>`, the browser collapses every newline into whitespace and renders thousands of characters of raw `##`, `**`, `` ` ``, `- `, and `1.` syntax inline inside a single history step.

## Branch Baseline Note

The `work/issue-920-plan-1` worktree was branched from `main` (`50091f80`), which holds the legacy tracker-only `scripts/issue_page.py`. The store-backed task page (`render_issue(item, ..., db=...)`), `scripts/store.py`, `scripts/reconcile.py`, `scripts/worker.py`, and `evals/test_m6_store_board.py` live on `origin/develop` (`4417a8fe`). Implementation should branch from or rebase onto `origin/develop`.

## Architectural Seam & Design Decision

Per `architecture/ARCHITECTURE.md` §4 (Dependency Rule), `scripts/issue_page.py` and `scripts/page_common.py` are Interface Adapters (presenters). Addressing this in `scripts/issue_page.py` (with CSS in `scripts/page_common.py` if needed) rather than truncating `LogEntry.message` in `reconcile.py` / `worker.py` is required for three reasons:

1. **Backwards compatibility with existing `store.db` data**: Live boards (including the two repros on `drago #2` and `chief-of-stuff #688`) already have the full `LogEntry.message` stored in SQLite. Fixing presentation in `scripts/issue_page.py` immediately repairs all historical task pages upon re-render without a database migration.
2. **Preserving existing engine/worker contracts**: `evals/test_reconcile.py:920` and `evals/test_worker.py:735-737` explicitly assert that `Close.reason` and `db.log(KEY)[-1].message` contain `"suite is not green; Suite: ...; worker said: ..."`.
3. **Full report remains accessible on the page**: Keeping the full text in `LogEntry.message` (and `db.reports(...)`) allows the presenter to show a concise one-line step summary by default while placing the worker's full report inside a collapsed `<details>` element rendered with `page_common.markdown_blocks`.

## Proposed Implementation

### 1. Parse and Format Worker Log Entries in `scripts/issue_page.py`

In `scripts/issue_page.py`, replace the raw `escape(entry.message)` mapping in `render_issue` with a dedicated presenter helper `_format_log_step(message: str, resolve: Callable[[str], str | None]) -> str`.

#### Step Pattern Matching

Match worker completion log entries against:

```python
_WORKER_LOG_RE = re.compile(
    r"^worker (?P<stage>\S+) attempt (?P<attempt>\d+): (?P<outcome>[^:\n]+?)(?:: (?P<reason>.*))?\Z",
    re.S,
)
```

- **Non-matching log entries** (or worker entries with no `reason`):
  Render as a single-line `<span>{escape(message)}</span>` (unchanged).

- **Case A — Suite gate / `; worker said: ` entries**:
  When `reason` contains `"; worker said: "`:
  1. Split `reason` on the first `"; worker said: "` into `gate_part` and `worker_report`.
  2. From `gate_part`, strip the verbose `"; Suite: ..."` clause (e.g. `gate_part.split("; Suite: ", 1)[0]`) to obtain the concise `short_reason` (e.g. `"suite is not green"`), while keeping the full `Suite: ...` detail for the expanded view if needed.
  3. The one-line step label becomes:
     `worker <stage> attempt <attempt>: <outcome>: <short_reason>`
     (for `drago #2`: `worker plan attempt 2: human_review: suite is not green`, matching the issue's expected text verbatim).
  4. If `worker_report.strip()` is non-empty, render it inside a collapsed `<details class="step-report">` block below the one-line summary using `markdown_blocks(worker_report, resolve)` (with the `Suite: ...` line included as a compact header or paragraph inside the details block when present).

- **Case B — Multi-line, Markdown-block, or long `reason` (e.g. `blocked`, `failed`, `human_review`)**:
  When `reason` does not contain `"; worker said: "` but either:
  - contains a newline (`"\n" in reason`),
  - exceeds a single-line threshold (`len(reason) > 140`), or
  - starts with block Markdown syntax (e.g. `#`, `- `, `* `, `1. `, ```` ``` ````):
  1. Extract `short_reason` from the first non-empty line of `reason`:
     - Strip leading Markdown heading markers (`^#{1,6}\s+`), list markers (`^(?:[-*+]|\d+\.)\s+`), and bold/code wrappers on the heading line.
     - If the first line still exceeds 120 characters, truncate cleanly at a sentence or word boundary and append `…`.
  2. The one-line step label becomes:
     `worker <stage> attempt <attempt>: <outcome>: <short_reason>`
  3. Render the full `reason` inside the collapsed `<details class="step-report">` block using `markdown_blocks(reason, resolve)`.

- **Case C — Short single-line `reason` (e.g. `"no report call"`, `"stale working tree"`, `"suite is not green"`)**:
  Render on one line with `markdown_inline` / `escape` and no `<details>` toggle.

### 2. HTML Structure & CSS (`scripts/issue_page.py` and `scripts/page_common.py`)

In `render_issue` (`scripts/issue_page.py`):
- Change the `.step` container's third child from `<span>{text}</span>` to `<div class="step-body">{text}</div>` (or keep `<span>` for simple steps and `<div class="step-body">` for all steps so block elements like `<details>` and `<div class="prose">` are valid HTML5 and never nested inside `<span>` or `<p>`).
- Structure of a step with a collapsed report:
  ```html
  <div class="step">
    <span class="when">18:49</span>
    <span class="dot"></span>
    <div class="step-body">
      <span>worker plan attempt 2: human_review: suite is not green</span>
      <details class="step-report">
        <summary>Worker report</summary>
        <div class="prose">...rendered markdown_blocks...</div>
      </details>
    </div>
  </div>
  ```
- Add minimal CSS rules in `scripts/page_common.py` (`BASE_CSS`) for `.step-body` and `details.step-report` so:
  - `.step-body` takes `min-width: 0; flex: 1` inside `.step`.
  - `details.step-report` has compact spacing (`margin-top: 4px`), a subtle summary toggle (`font-size: 12px; color: var(--muted); cursor: pointer`), and a bounded/styled `.prose` container that matches `.Verdict` / `.Description`.
  - Verify `Structure` HTML parser in `evals/test_page_markdown.py` reports `blocks_in_p == []` (no `<div>`, `<pre>`, `<ul>`, `<ol>`, `<h1>`–`<h6>` inside `<p>`).

### 3. Coordinate with Peer Plans Touching `scripts/issue_page.py`

Other active issues (`#911`, `#913`, `#915`, `#919`) also modify `scripts/issue_page.py` around `How it got here` (`steps_list`). Keeping the worker-log formatting encapsulated in a small pure helper `_format_log_step(entry.message, linker.resolve)` in `scripts/issue_page.py` minimizes merge conflicts with timestamp/order/duration changes in `steps_list`.

## Verification & Test Plan (TDD)

1. **Red — Write failing tests first** in `evals/test_m6_store_board.py` (and/or `evals/test_issue_page.py`):
   - **Suite-not-green with long Markdown `worker said:` report (`drago #2` shape)**:
     Append a `LogEntry` via `db.append_log(key, "worker plan attempt 2: human_review: suite is not green; Suite: passed=missing or invalid, failed=0, skipped=0, xfailed=0; worker said: ## Implementation Plan\n\n### 1. Root Cause\n- **File**: `src/app.py`\n1. First step\n2. Second step\n" + ("detail line\n" * 80))`.
     Render with `issue_page.render_issue(item, NOW, db=db)`.
     Assert:
     - The visible one-line step text contains `"worker plan attempt 2: human_review: suite is not green"`.
     - Raw Markdown markers (`"## Implementation Plan"`, `"### 1. Root Cause"`, `"**File**"`) do **not** appear as literal unrendered syntax in the page text.
     - A `<details class="step-report">` element is present (collapsed by default, i.e., no `open` attribute) and contains rendered HTML (`<h2>`, `<h3>`, `<ul>`, `<ol>`, `<strong>`, `<code>`) inside `<div class="prose">`.
     - No block elements (`<div>`, `<h2>`, `<ul>`, `<ol>`, `<pre>`) are nested inside `<p>` or `<span>`.
   - **Multi-line `blocked` report (`chief-of-stuff #688` shape)**:
     Append a `LogEntry` with `"worker plan attempt 2: blocked: Blocked as stated in issue #688 (\"Blocked on #683, #684\").\n\n### Why #688 Cannot Proceed Yet\n1. **Missing module**: `scripts/store.py`..."`.
     Render with `issue_page.render_issue(item, NOW, db=db)`.
     Assert:
     - The one-line step summary shows `"worker plan attempt 2: blocked: Blocked as stated in issue #688"` (single line).
     - The full report is rendered inside `<details class="step-report">` with `<h3>`, `<ol>`, `<strong>`, `<code>`, and linked `#688` / `#683`.
   - **Short single-line worker log entry**:
     Append `"worker plan attempt 1: human_review: no report call"`.
     Assert it renders on one line with no `<details class="step-report">`.

2. **Green — Implement `_format_log_step` and CSS**:
   - Implement the helper in `scripts/issue_page.py` and CSS in `scripts/page_common.py`.
   - Run the full test suite (`python3 -m unittest discover -s evals`) and verify `passed > 0` and `failed = skipped = xfailed = 0`.

## Risks & Mitigations

- **Risk**: `markdown_blocks` output placed inside a `<span>` or `<p>` produces invalid HTML and fails `test_page_markdown.py`'s `Structure` check.
  - **Mitigation**: Use `<div class="step-body">` for `.step`'s content wrapper and `<div class="prose">` inside `<details class="step-report">`. Validate with `Structure` in unit tests.
- **Risk**: Existing tests in `evals/test_m6_store_board.py` check for substrings in `render_issue` output.
  - **Mitigation**: Keep short single-line log entries (`"queued"`, `"worker plan attempt 1: done"`, `"worker plan attempt 1: human_review: no report call"`) rendering identically, and preserve the `"worker <stage> attempt <attempt>: <outcome>: <short_reason>"` prefix on long entries.

## File Ownership (for `impl` stage)

- `scripts/issue_page.py` (`write`)
- `scripts/page_common.py` (`write`)
- `evals/test_m6_store_board.py` (`write`)
- `evals/test_issue_page.py` (`read`)
- `evals/test_page_markdown.py` (`read`)
- `scripts/reconcile.py` (`read`)
- `scripts/worker.py` (`read`)
- `scripts/engine.py` (`read`)

```logic
P1: LogEntry.message in store.db embeds full multi-line Markdown worker reports via reconcile.suite_reason ("; worker said: <summary>") and worker.collect (non-done report.summary)
P2: scripts/issue_page.py renders db.log(item.issue_key) directly inside <span>{escape(entry.message)}</span> without truncating the step line or rendering Markdown
P3: Live store.db instances and existing unit tests (test_reconcile.py, test_worker.py) depend on LogEntry.message retaining the full reason string
---
C1: P1 & P2 -> Task history pastes thousands of chars of raw Markdown into one step on the task page
C2: C1 & P3 -> scripts/issue_page.py must split worker completion log entries into a one-line step summary and a collapsed <details> block rendered with page_common.markdown_blocks
```
