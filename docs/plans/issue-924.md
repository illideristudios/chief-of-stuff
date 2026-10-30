# Issue #924: Read-only answered decision page and server refusal of duplicate answers

## Problem

On a standalone decision page (`/decisions/<slug>`, rendered by `scripts/decision_page.py` and served by `scripts/pages.py`), an already-answered decision still renders the live `<form class="options" method="post" action="/decisions/<slug>">` containing radio inputs for each option, the write-in radio and text input (`name="words"`), the `save` submit button, and `ANSWER_JS` (which posts immediately on radio `change`).

In addition:
1. When a decision is answered via a tracker `## Decisions` row (`ctx.answer(slug)`) or a string key in `d["answer"]` rather than a page-saved dict `saved(d)`, `render()` does not even render `<div class="card answered">` above the options.
2. In `scripts/decision_page.py` (`render()`, lines 408–409 on `50091f80` / lines 398–399 on `origin/develop` `4417a8fe`), `row = ctx.answer(slug) if ctx and not answer_key(d) else None` and `chosen = answer_key(d) or (ctx.key(row, keys) if row else "")` give precedence to a page-saved answer over the tracker's `## Decisions` row, contradicting `saved()`'s own contract (*"until a Decisions row takes it up"*) and `index()` (which evaluates `ctx.key(row, keys) or answer_key(d)`).
3. In `scripts/pages.py` (`Handler.do_POST`, lines 358–375 on `50091f80` / lines 507–524 on `origin/develop` `4417a8fe`), `POST /decisions/<slug>` parses `decision-<slug>.json` and overwrites `d["answer"]` unconditionally without checking whether `d` already has an answer in JSON or in a tracker `## Decisions` row.

As a result, a single click on any radio option on an answered decision page immediately posts to `POST /decisions/<slug>`, overwrites `d["answer"]` in `decision-<slug>.json`, and flips the rendered answer and header chip.

## Design and Implementation Sequence

### 1. `scripts/decision_page.py`: Answer resolution, read-only options, and public answer check

1. **Add public helpers `tracker_answer` and `is_answered`** (public names so `scripts/pages.py` imports no underscore-prefixed symbol, complying with `architecture/ARCHITECTURE.md` §3):
   ```python
   def tracker_answer(root: Path | None, slug: str) -> Row | None:
       """The latest Decisions row naming `decision-<slug>` across the workspace's daily trackers, or None."""
       if root is None or not slug:
           return None
       try:
           cfg = read_config(root)
           rows = [Row(d, leading(t, d, cfg.zone), item, words)
                   for d, path in daily_trackers(root, cfg)
                   for t, item, words in decision_rows(path.read_text())]
       except (OSError, ConfigError):
           return None
       return Context(datetime.now(cfg.zone), tuple(rows)).answer(slug)


   def is_answered(d: dict, slug: str = "", root: Path | None = None, ctx: Context | None = None) -> bool:
       """True when `d` carries an answer or a tracker Decisions row names `decision-<slug>`."""
       if d.get("answer"):
           return True
       row = ctx.answer(slug) if ctx is not None else tracker_answer(root, slug)
       return row is not None
   ```

2. **Format the recorded answer card for all answer sources (`_answered_html`)**:
   Keep `_saved_html(d, when)` for `index()` (`decisions.html` pending rows with a page-saved answer), and add `_answered_html(d: dict, when, chosen: str = "", row: Row | None = None, link: Linker = Linker()) -> str` for `render()`:
   - When `row is None` and `saved(d)` is a dict, delegate to `_saved_html(d, when)` so page-saved answers keep their exact existing markup (`<span><span class="key">...</span>...</span><span class="words">“...”</span><span class="saved">answered, saved HH:MM</span>`).
   - When `row` is present (from `ctx.answer(slug)`), or when `d["answer"]` is a string option key:
     - Render the chosen option badge and title when `chosen` is non-empty:
       `f'<span><span class="key">{escape(chosen)}</span>{escape(title)}</span>'`
     - When `row` has non-empty words (`raw = row.words.strip().strip('"“”').strip()`), or falls back to `str((saved(d) or {}).get("words") or "").strip()`, render:
       `f'<span class="words">“{_md(raw, link)}”</span>'`
     - Render the status/timestamp span:
       `f'<span class="saved">answered {when(row.when)}</span>'` when `row and row.when` else `'<span class="saved">answered</span>'`.

3. **Resolve `row`, `chosen`, and `answered` before building the `Options` section in `render()`**:
   - Compute:
     ```python
     keys = [o["key"] for o in d["options"]]
     page = saved(d)
     row = ctx.answer(slug) if ctx else None
     chosen = (ctx.key(row, keys) if row else "") or answer_key(d)
     answered = bool(chosen or row or page)
     ```
     This ensures a tracker `## Decisions` row always outranks a page-saved answer in `d["answer"]`, matching `index()` (`ctx.key(row, keys) or answer_key(d)`).
   - When `answered` is `True`:
     - Render each option in `d["options"]` as a read-only `<div class="opt">`:
       ```python
       opts.append(f'      <div class="opt"><span class="k">{escape(o["key"])}</span>'
                   f'<b>{escape(o["title"])}{tag}</b>\n        <span class="d">{md(o.get("text", ""))}</span>\n      </div>')
       ```
     - Omit the write-in option (`Your own answer`, `<input type="text" name="words">`, `<button type="submit">save</button>`).
     - Render `<div class="card answered">{_answered_html(d, fmt, chosen, row, link)}</div>` above `<div class="options">\n...\n    </div>` (no `<form>`).
     - In `.foot`, render `f"Prepared by the coordinator{sources}."` without `"Answer here or in chat: ..."`, and omit `ANSWER_JS` before `</body>`.
   - When `answered` is `False`:
     - Preserve the existing `<form class="options" method="post" action="/decisions/{escape(slug)}">` with `<label class="opt">`, `<input type="radio" name="key" ...>`, the write-in option `wi_key`, `<input type="text" name="words" aria-label="Your own answer">`, `<button type="submit">save</button>`, the footer prompt, and `ANSWER_JS`.
   - In `PAGE_CSS`, change `.opt:hover{background:var(--bg)}` to `label.opt:hover{background:var(--bg)}` so read-only `<div class="opt">` items do not highlight on hover as interactive controls.

### 2. `scripts/pages.py`: Refuse `POST /decisions/<slug>` when already answered

In `Handler.do_POST` (the `ROUTE.fullmatch` branch for `/decisions/<slug>`), inside `with page_lock(Path(self.directory) / f"decision-{m[1]}.html"):`:
- Immediately after `d = decision_page.parse(json.loads(source.read_text()))`, check whether the decision is already answered:
  ```python
  if decision_page.is_answered(d, m[1], getattr(self.server, "root", None)):
      return self.send_error(409, "the decision is already answered")
  ```
- When refused, return HTTP `409 Conflict` without modifying `decision-<slug>.json`, its mtime, or `RENDERED_AT`.

## Test Plan (TDD)

1. **Red — Update and add failing tests in `evals/test_decision_page.py`**:
   - **`RenderTest.test_a_saved_page_answer_shows_the_decision_answered`**:
     - For both a keyed saved answer (`{"key": "B", "words": "B, and log it", "at": ...}`) and a write-in saved answer (`{"key": None, "words": "neither; ask the owner", "at": ...}`), assert that `<div class="card answered">` and `<div class="options">` are present, the option titles and descriptions remain visible in `<div class="opt">`, and `<form`, `type="radio"`, `name="words"`, `Your own answer`, and `<button` are all absent.
   - **`DesignTest.test_the_chip_says_pending_until_the_answer_is_recorded`**:
     - Assert that `self.page(answer="B")` renders `<div class="card answered"><span><span class="key">B</span>Follow the plan</span><span class="saved">answered</span></div>` and contains no `<form`, `type="radio"`, `name="words"`, or `<button`.
   - **`DerivedPageTest.test_asked_answer_and_the_chip_come_from_the_tracker` and `MainTest.test_the_cli_reads_asked_and_the_answer_from_the_trackers`**:
     - Assert that a decision answered by a tracker `## Decisions` row renders `<div class="card answered">` with `<span class="key">B</span>Follow the plan`, the quoted user words (`“B”` / `“B, go”`), and `<span class="saved">answered 01:15</span>` (or `answered 10:05`), and contains no `<form`, `type="radio"`, `name="words"`, or `<button`.
   - **`DerivedPageTest.test_a_decisions_row_renders_read_only_and_outranks_a_saved_page_answer`** (new test):
     - Verify that a free-form `## Decisions` row naming `decision-x` whose words do not name an option key (e.g. `"let's go ahead and fix the toml issues preventing page rendering please"`) marks the chip `ANSWERED`, renders the words and timestamp in `<div class="card answered">`, and omits `<form`, `type="radio"`, `name="words"`, and `<button`.
     - Verify that when `d` carries a saved answer for option `"A"` (`{"key": "A", "words": "old", "at": ...}`) and a tracker `## Decisions` row for `decision-x` chooses `"B"`, the header chip reads `ANSWERED · B` and `<div class="card answered">` shows `B` (`Follow the plan`) and the tracker row's words rather than `"A"`.

2. **Red — Add failing server tests in `evals/test_pages.py`**:
   - **`AnswerTest.test_a_choice_is_saved_and_the_page_shows_it_answered`**:
     - Assert that the rendered `/decisions/cache-ttl` page after saving has no `<form`, `type="radio"`, `name="words"`, or `<button`.
   - **`AnswerTest.test_an_answered_decision_refuses_a_second_answer`** (new test):
     - Save an initial answer (`key=B&words=first`) via `POST /decisions/cache-ttl` (303).
     - Post a second answer (`key=A&words=second`) to `POST /decisions/cache-ttl`; assert status `409`, and verify `self.answer()` in `decision-cache-ttl.json` still has `key == "B"` and `words == "first"`.
     - Test with a pre-existing string `"answer": "A"` in `decision-cache-ttl.json`; assert `POST /decisions/cache-ttl` with `b"key=B"` returns `409` and leaves `"answer": "A"` untouched.
   - **`AnswerTest.test_a_decision_answered_in_the_tracker_refuses_a_page_post`** (new test):
     - Write a `## Decisions` row naming `decision-cache-ttl` in `self.tracker` (with `self.json` having no `"answer"` key).
     - Verify `GET /decisions/cache-ttl` shows `ANSWERED` with `<div class="card answered">` and no `<form` or `type="radio"`.
     - Post `b"key=B"` and `b"words=override"` to `POST /decisions/cache-ttl`; assert status `409` and `self.answer() is None`.

3. **Green — Implement and verify**:
   - Implement the changes in `scripts/decision_page.py` and `scripts/pages.py`.
   - Run with an isolated `TMPDIR` and system `git` ahead of the worker guard shim on `PATH`:
     ```bash
     TMP=$(mktemp -d) && TMPDIR="$TMP" PATH="/opt/homebrew/bin:/usr/bin:$PATH" python3 -m unittest evals/test_decision_page.py evals/test_pages.py evals/test_architecture.py && rm -rf "$TMP"
     ```

## Risks and Mitigations

- **Worker `git` guard shim and shared `TMPDIR` during unit tests**:
  - `ModuleGraphTest` in `evals/test_decision_page.py` invokes `git -c core.hooksPath=/dev/null ... fetch`, which the worker session's `git` guard shim on `PATH` rejects, and `FreshRefusesAForeignNameTest` in `evals/test_pages.py` snapshots `self.root.parent` (`TMPDIR`), which fails if concurrent processes write into the shared system temp directory. Running tests with `TMP=$(mktemp -d)` (`TMPDIR="$TMP"`) and `PATH="/opt/homebrew/bin:/usr/bin:$PATH"` keeps both suites deterministic and green (169 passed, 0 failed, 0 skipped, 0 xfailed).
- **Branch divergence between `50091f80` (`main`) and `origin/develop`**:
  - `scripts/decision_page.py` on `origin/develop` delegates `_md` to `page_common.markdown_inline` instead of the local regex. Using `_md(raw, link)` in `_answered_html` works identically on both branches and handles non-string `words` values cleanly via `str(...)`.
- **Clean Architecture import boundaries (`architecture/ARCHITECTURE.md` §3)**:
  - `pages.py` calls only the public function `decision_page.is_answered(...)`, never an underscore-prefixed helper.

## File Ownership

| Path | Mode | Reason |
| --- | --- | --- |
| `scripts/decision_page.py` | `write` | Render answered decisions read-only without `<form>`, radios, write-in, or save; prefer `Decisions` row over page-saved answer; expose `tracker_answer` and `is_answered`. |
| `scripts/pages.py` | `write` | Refuse `POST /decisions/<slug>` with HTTP 409 when the decision is already answered in JSON or in a tracker `Decisions` row. |
| `evals/test_decision_page.py` | `write` | Update answered-page rendering assertions and add regression tests for tracker-answered and overridden decisions. |
| `evals/test_pages.py` | `write` | Add regression tests refusing `POST /decisions/<slug>` when answered on the page, in JSON, or in a tracker `Decisions` row. |
| `architecture/ARCHITECTURE.md` | `read` | Verify presenter/driver dependency direction and public-name import rule (§3, §4). |
| `evals/test_architecture.py` | `read` | Verify module classification and import graph rules. |
