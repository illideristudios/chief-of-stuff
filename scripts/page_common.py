"""Shared page prose renderer (architecture §3), in Interface Adapters.

The page supplies the forge resolver; generic chart presenters receive an inline renderer as a function.
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable

from _vendor.markdown_it import MarkdownIt
from fragment import esc, href

# References recognized in prose text tokens. Order is precedence.
TOKEN = re.compile(r"""
    (?P<tag><[^>]+>)
  | `(?P<code>[^`]+)`
  | (?<![\w/>])(?P<url>https?://[^\s<`]*[^\s<`.,;:)])
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


def _prose_text(text: str, resolve: ReferenceResolver | None) -> str:
    """Escape text and link bare URLs and forge references, never scanning generated markup."""
    out, end = [], 0
    for match in TOKEN.finditer(text):
        out.append(html.escape(text[end:match.start()]))
        if match['url']:
            out.append(f'<a {href(match["url"])}>{html.escape(match["url"], quote=False)}</a>')
        else:
            out.append(_chip(match, resolve) or html.escape(match[0]))
        end = match.end()
    out.append(html.escape(text[end:]))
    return ''.join(out)


def _prose_references(state) -> None:
    """Decorate parsed prose tokens, leaving explicit links, images and code blocks intact."""
    resolve = state.env.get('resolve')
    for block in state.tokens:
        if block.type != 'inline':
            continue
        depth = 0
        for token in block.children or ():
            if token.type == 'link_open':
                depth += 1
                url = token.attrGet('href') or ''
                if url.lower().startswith(('http://', 'https://')):
                    token.attrSet('target', '_blank')
                    token.attrSet('rel', 'noopener')
            elif token.type == 'link_close':
                depth -= 1
            elif depth == 0 and token.type == 'text':
                token.type, token.content = 'html_inline', _prose_text(token.content, resolve)
            elif depth == 0 and token.type == 'code_inline' and resolve:
                match = TOKEN.fullmatch(token.content)
                chip = _chip(match, resolve) if match else None
                if chip:
                    token.type, token.content = 'html_inline', chip


_MARKDOWN = MarkdownIt('commonmark').enable('table')
_MARKDOWN.options['html'] = False
_MARKDOWN.core.ruler.after('text_join', 'prose_references', _prose_references)
# Keep the pages' existing bold markup and styling.
_MARKDOWN.renderer.rules['strong_open'] = lambda *args: '<b>'
_MARKDOWN.renderer.rules['strong_close'] = lambda *args: '</b>'


def markdown_inline(text: str, resolve: ReferenceResolver | None = None) -> str:
    """One-line prose: inline CommonMark only, with raw HTML escaped."""
    return _MARKDOWN.renderInline(str(text), {'resolve': resolve})


def markdown_blocks(text: str, resolve: ReferenceResolver | None = None) -> str:
    """Block prose: CommonMark plus tables, with raw HTML escaped."""
    return _MARKDOWN.render(str(text), {'resolve': resolve})
