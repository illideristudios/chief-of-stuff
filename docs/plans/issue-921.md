# Issue 921: drop HTML comments from review bodies

## Problem

The Review section and the source page print a Codex marker as text. GitHub hides `<!-- codex-code-review-finding:v1 -->`. The board shows it once per thread (#208 ten times, #231 twice, and twice on the #231 source page).

## Cause

`scripts/page_common.py` renders review bodies with CommonMark and `html: False`. A comment is ordinary paragraph text, and the renderer escapes it. On `origin/develop` that body renders as the crawl recorded:

```html
<p>Useful? React with 👍 / 👎.</p>
<p>&lt;!-- codex-code-review-finding:v1 --&gt;</p>
```

Both call sites pass the raw body straight in:

- `scripts/issue_page.py` `_review` → `markdown_blocks(t.body, linker.resolve)`
- `scripts/source_page.py` `_note` → `markdown_blocks(t.body, linker.resolve)`

This plan worktree is `50091f80`, which still wraps the body in one `html.escape` paragraph and has no `page_common.py`. That is not the page the crawl saw. Implement on a tree that contains `scripts/page_common.py`.

## Change

Add `drop_html_comments` in `page_common.py` and call it on the review body only, before `markdown_blocks`, at those two sites. Leave `markdown_blocks`, `_MARKDOWN.options['html']`, and `scripts/_vendor/` alone. Issue bodies and decision prose keep the #231 rule: raw HTML, comments included, stays escaped.

Fences are tried before inline code so a ` ``` ` fence is not consumed as a double-backtick span.

```python
_HTML_COMMENT = re.compile(
    r"(?P<code>^[ ]{0,3}```.*?^[ ]{0,3}```|^[ ]{0,3}~~~.*?^[ ]{0,3}~~~|``.+?``|`[^`\n]+`)"
    r"|(?P<comment><!--.*?-->)",
    re.DOTALL | re.MULTILINE,
)

def drop_html_comments(text: str) -> str:
    """Drop HTML comments the forge hides. A comment inside a code span or fence stays."""
    return _HTML_COMMENT.sub(lambda m: m.group(0) if m.group("code") else "", text)
```

Stripping before render lets CommonMark collapse the leftover blank lines, so the marker paragraph is gone rather than an empty `<p>`.

## Behaviour

- A closed comment in a review body, including one that spans lines, is removed. The sentence before the Codex marker stays. The same body on the source page loses the marker.
- Every other raw tag stays escaped. `<script>` and `<img ...>` render as text, never as live tags.
- A comment inside a fence (` ``` `, `~~~`, ` ```suggestion `, indent 0–3) or an inline code span stays, escaped, because the forge shows it.
- Every closed comment in one body is removed.

## Tests

Red first, then the helper and the two call sites.

1. `evals/test_issue_page.py` `ReviewTest`. One thread body is the crawl text, a `<script>alert(1)</script>` paragraph, and a fenced `<!-- keep -->`. The Review section contains the sentence and `&lt;script&gt;`, contains neither `codex-code-review-finding` nor a live `<script>`, and still shows `&lt;!-- keep --&gt;` inside the code element.
2. `evals/test_source_notes.py`. A note that ends with the marker paragraph does not contain the marker. `test_thread_text_is_shown_as_text` stays green: the `<img>` payload is still escaped text.
3. `evals/test_page_markdown.py`. `markdown_blocks` of a bare comment still contains `&lt;!--`. That locks the strip out of the shared renderer.

`python3 -m unittest evals.test_issue_page evals.test_source_notes evals.test_page_markdown`

## Out of scope

Comments in issue bodies, acceptance lines, log lines, and decision prose. Enabling raw HTML. Editing the vendored markdown library.

## Limits

- An unclosed `<!--` (no `-->`) stays visible. The Codex marker is closed. Upgrade when a bot emits an unclosed comment.
- A comment in an indented code block (four spaces, not a fence) is dropped. Review bodies use fences. Upgrade when a thread shows the marker there.
- A comment mid-sentence leaves a double space (`hello <!-- x --> world`). HTML collapses it.

## File ownership

Write: `scripts/page_common.py`, `scripts/issue_page.py`, `scripts/source_page.py`, `evals/test_issue_page.py`, `evals/test_source_notes.py`, `evals/test_page_markdown.py`.
