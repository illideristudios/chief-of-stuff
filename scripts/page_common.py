"""Shared page prose renderer (architecture §3), in Interface Adapters.

The page supplies the forge resolver; generic chart presenters receive an inline renderer as a function.
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable
from threading import RLock

from _vendor.markdown_it import MarkdownIt
from _vendor.markdown_it.token import Token
from fragment import esc, href

# References recognized in prose text tokens. Order is precedence.
TOKEN = re.compile(r"""
    (?<![\w/>])(?P<url>https?://[^\s<`]*[^\s<`.,;:)])
  | \b(?:[Pp]ipeline|[Rr]un)\ \#(?P<pid>\d+)\b
  | (?<![\w&/!\#])(?P<sigil>[!\#])(?P<num>\d+)(?:\#(?P<note>note_\d+|issuecomment-\d+|discussion_r\d+))?\b
  | (?<![\w/.-])(?P<path>(?:[\w.-]+/)+(?![\w.-])|(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z]\w*)
    (?::(?P<line>\d+)(?:[-–](?P<end>\d+))?\b|:(?P<sym>[A-Za-z_]\w*)\b|\ §\ ?(?P<sec>\d+(?:\.\d+)*)|\#(?P<anchor>[\w-]+))?
  | (?<![\w\#&-])(?P<sha>(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40})\b
""", re.X)

# A page supplies its forge resolver; charts can render prose without knowing any workspace type.
ReferenceResolver = Callable[[re.Match], tuple[str, str, str] | None]


def _chip(match: re.Match, resolve: ReferenceResolver | None) -> str | None:
    ref = resolve(match) if resolve else None
    if not ref:
        return None
    kind, label, url = ref
    return f'<a class="ref" data-k="{esc(kind)}" {href(url)}>{html.escape(label, quote=False)}</a>'


def _prose_text(text: str, resolve: ReferenceResolver | None, literal: frozenset[int] = frozenset()) -> str:
    """Escape text and link bare URLs and forge references, never scanning generated markup.
    A sigil at an index in `literal` came from an escape or entity and stays plain text."""
    out, end = [], 0
    for match in TOKEN.finditer(text):
        out.append(html.escape(text[end:match.start()]))
        if match['sigil'] and match.start('sigil') in literal:
            out.append(html.escape(match[0]))
        elif match['url']:
            out.append(f'<a {href(match["url"])}>{html.escape(match["url"], quote=False)}</a>')
        else:
            out.append(_chip(match, resolve) or html.escape(match[0]))
        end = match.end()
    out.append(html.escape(text[end:]))
    return ''.join(out)


def _prose_references(state) -> None:
    """Decorate parsed prose tokens, leaving explicit links, images and code blocks intact."""
    resolve = state.env.get('resolve')
    links = state.env.get('links', True)
    for block in state.tokens:
        if block.type != 'inline':
            continue
        depth, run, children = 0, [], []

        def flush() -> None:
            # An escape or entity splits a run into tokens; scan the run whole so a reference keeps its boundaries.
            if run:
                text, literal = '', set()
                for t in run:
                    if t.type == 'text_special':
                        literal.update(range(len(text), len(text) + len(t.content)))
                    text += t.content
                children.append(Token('html_inline', '', 0, content=_prose_text(text, resolve, frozenset(literal))))
                run.clear()

        for token in block.children or ():
            if links and depth == 0 and token.type in ('text', 'text_special'):
                run.append(token)
                continue
            flush()
            children.append(token)
            if token.type == 'link_open':
                depth += 1
                if not links:
                    token.type, token.content = 'html_inline', ''
                    continue
                url = token.attrGet('href') or ''
                if url.lower().startswith(('http://', 'https://')):
                    token.attrSet('target', '_blank')
                    token.attrSet('rel', 'noopener')
            elif token.type == 'link_close':
                depth -= 1
                if not links:
                    token.type, token.content = 'html_inline', ''
            elif links and depth == 0 and token.type == 'code_inline' and resolve:
                match = TOKEN.fullmatch(token.content)
                chip = _chip(match, resolve) if match else None
                if chip:
                    token.type, token.content = 'html_inline', chip
        flush()
        block.children = children


_MARKDOWN = MarkdownIt('commonmark').enable('table')
_MARKDOWN.options['html'] = False
_MARKDOWN.core.ruler.before('text_join', 'prose_references', _prose_references)
# Keep the pages' existing bold markup and styling.
_MARKDOWN.renderer.rules['strong_open'] = lambda *args: '<b>'
_MARKDOWN.renderer.rules['strong_close'] = lambda *args: '</b>'
# The threaded page server shares this parser, whose ruler caches compile lazily.
_MARKDOWN_LOCK = RLock()

# Code spans use a matching run of backticks; code blocks are protected using the block parser below.
_COMMENT_OR_CODE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`).*?(?<!`)\2(?!`))|(?P<comment><!--.*?-->)",
    re.DOTALL,
)


def drop_html_comments(text: str) -> str:
    """Drop complete forge-hidden comments, preserving literal comments in Markdown code."""
    def block_ranges(source: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        offsets = [0]
        for line in source.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        blocks = MarkdownIt('commonmark', {'html': True}).parse(source)
        def ranges(types: tuple[str, ...]) -> list[tuple[int, int]]:
            return [(offsets[t.map[0]], offsets[t.map[1]]) for t in blocks if t.type in types and t.map]
        return ranges(('fence', 'code_block')), ranges(('inline',))

    protected, inline = block_ranges(text)
    pos = 0
    while pos < len(text):
        block = next(((start, end) for start, end in protected if end > pos), None)
        if block and block[0] <= pos:
            pos = block[1]
            continue
        match = _COMMENT_OR_CODE.search(text, pos)
        if block and (match is None or block[0] <= match.start() or
                      (match.group('code') and block[0] < match.end())):
            # An inline code span cannot cross a block boundary.
            match = _COMMENT_OR_CODE.search(text, pos, block[0])
            if match is None:
                pos = block[1]
                continue
        if match is None:
            break
        if match.group('code') and not any(start <= match.start() and match.end() <= end for start, end in inline):
            pos = match.start() + 1
            continue
        backslashes = len(text[:match.start()]) - len(text[:match.start()].rstrip('\\'))
        if backslashes % 2:
            pos = match.start() + 1
            continue
        if match.group('comment'):
            # Keep offsets and line boundaries stable; reparse because hidden text can contain fake fences.
            masked = re.sub(r'[^\r\n]', ' ', match.group(0))
            text = text[:match.start()] + masked + text[match.end():]
            protected, inline = block_ranges(text)
        pos = match.end()
    return text


def identify(page: bytes, label: str) -> bytes:
    """Name the workspace in a served page's `<title>` and first `<h1>`, so a tab, history entry or screenshot says
    which board it is. The label is escaped here; a page without either tag is left as it was."""
    prefix = html.escape(label, quote=False).encode() + " · ".encode()
    for tag in (rb"<title\b[^>]*>", rb"<h1\b[^>]*>"):
        page = re.sub(tag, lambda m: m[0] + prefix, page, count=1, flags=re.I)
    return page


def markdown_inline(text: str, resolve: ReferenceResolver | None = None, *, links: bool = True) -> str:
    """Inline CommonMark with raw HTML escaped; `links=False` keeps a navigation label free of anchors."""
    with _MARKDOWN_LOCK:
        return _MARKDOWN.renderInline(str(text), {'resolve': resolve, 'links': links})


def markdown_blocks(text: str, resolve: ReferenceResolver | None = None) -> str:
    """Block prose: CommonMark plus tables, with raw HTML escaped."""
    with _MARKDOWN_LOCK:
        return _MARKDOWN.render(str(text), {'resolve': resolve})
