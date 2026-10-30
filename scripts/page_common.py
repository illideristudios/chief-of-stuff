"""Shared page shell, atomic publication and prose renderer (architecture §3), in Interface Adapters.

The page supplies the forge resolver; generic chart presenters receive an inline renderer as a function.
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable
from threading import RLock
from pathlib import Path

from _vendor.markdown_it import MarkdownIt
from _vendor.markdown_it.token import Token
from fragment import FONTS, PROSE_CSS, TAB_CSS, esc, href
from workspace import swap_in

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


def write_if_changed(path: Path, text: str) -> None:
    """Write `text` to `path` unless it already holds exactly those bytes: an unchanged page keeps its mtime, which is
    its Last-Modified (#330). A changed one is swapped in by a rename, so a reader never sees it half written."""
    data = text.encode()
    try:
        if path.read_bytes() == data:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    swap_in(path, text, mode=0o644)


# The theme-carrying pages splice the canonical theme (fragment.theme, read at render time so a re-sync reaches
# the served pages without a restart) between the one `<style>` block's open and the pages' own rules below —
# cascade order (theme first, page rules second) is all that decides what wins. The pages that do not carry the
# theme yet take `HEAD` whole, which is the same block without the theme.
HEAD_OPEN = f'<link rel="stylesheet" href="{FONTS}">\n<style>\n'
PAGE_CSS = """/* The board's parchment scheme (render_board.py) and the architecture review's state encoding (frank-lloyd-aight
   docs/review-page.md). Okabe-Ito colours the timeline stages, as on the board Gantt. */
:root{--bg:#f4efe1;--surface:#fbf8ef;--fg:#2f2630;--muted:#6e6470;--line:#ddd3bd;--brass:#a7843e;--dl:#c9533a;--link:#8a6c30;--ref-bg:#efe7d3;--deployed:#2b3542;--inflight:#a8660f;--inflight-soft:#f6ead6;--designed:#2c58a0;--designed-soft:#dfe8f6;--ext:#8a8f98;--ext-soft:#f1eee6;--ext-ink:#5f6b7b;--stage-implement:#0072B2;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#000000;--now:#4f6b3a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--link:#e2c27f;--ref-bg:#342b1f;--deployed:#d9d2c4;--inflight:#e8ad55;--inflight-soft:#35281a;--designed:#8fb3f0;--designed-soft:#1f2735;--ext:#8f8a80;--ext-soft:#2d2822;--ext-ink:#c4bdb0;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2;--now:#a8c67e}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--link:#e2c27f;--ref-bg:#342b1f;--deployed:#d9d2c4;--inflight:#e8ad55;--inflight-soft:#35281a;--designed:#8fb3f0;--designed-soft:#1f2735;--ext:#8f8a80;--ext-soft:#2d2822;--ext-ink:#c4bdb0;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2;--now:#a8c67e}
*{box-sizing:border-box}
/* The canonical theme sits above the marker in this block (fragment.theme); these restate what its base rules would
   otherwise change: the pages keep their own scrolling, spacing, chips and figures. */
html{scroll-behavior:auto}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Alegreya Sans","Gill Sans",system-ui,sans-serif;padding:24px 16px 48px}
main{margin:0 auto;display:flex;flex-direction:column;gap:16px;padding:0}
h1,h2{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-weight:600;line-height:normal;text-wrap:balance;margin:0}
h1{font-size:27px;letter-spacing:.04em;margin-top:2px}
h2{font-size:16px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px}
p{margin:0}
a{color:var(--link);overflow-wrap:anywhere}
section{min-width:0;margin-top:0;scroll-margin-top:0}
header.top{display:flex;align-items:flex-end;gap:20px;border-bottom:1px solid var(--brass);padding-bottom:10px;margin-bottom:0}
header.top>div{flex:1 1 auto;min-width:0}
.eyebrow{font-size:12.5px;color:var(--muted);font-family:inherit;letter-spacing:normal;text-transform:none}
.card{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:10px 14px}
.card>p+p{margin-top:6px}
.cap{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.chip,.pill{display:inline;line-height:normal;vertical-align:baseline;font-family:inherit;text-transform:none;font-size:10.5px;font-weight:700;letter-spacing:.06em;border-radius:3px;padding:0 6px;white-space:nowrap}
header .chip{color:var(--link);border:1px solid var(--brass);margin-bottom:4px}
header .chip.answered{color:var(--surface);background:#009E73;border-color:#009E73}
.ask{border-top:3px solid var(--brass);display:flex;flex-direction:column;gap:4px}
.ask b{font-size:16px}
.ask p{color:var(--muted)}
.ask>.prose:first-of-type p{color:var(--fg);font-size:16px}
code{font-family:ui-monospace,monospace;font-size:.88em;background:var(--ref-bg);padding:0 3px;border-radius:3px;overflow-wrap:normal}
.ref{font-family:ui-monospace,monospace;font-size:.88em;color:var(--link);text-decoration:none;border-bottom:1px dotted var(--brass);background:var(--ref-bg);padding:0 3px;border-radius:3px;white-space:nowrap}
.ref::before{content:attr(data-k);font-size:.8em;color:var(--muted);margin-right:3px}
.cols{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:18px}
.col{display:flex;flex-direction:column;gap:14px;min-width:0}
.compare{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.side{display:flex;flex-direction:column;gap:4px;align-items:flex-start}
.pill.done{color:#fff;background:#009E73;border:0} .pill.open{color:#2f2630;background:#E69F00;border:0}
.timeline{padding:4px 14px}
.step{display:grid;grid-template-columns:70px 14px minmax(0,1fr);gap:0 10px;padding:6px 0}
.step+.step{border-top:1px dotted var(--line)}
.step .when{font-family:ui-monospace,monospace;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.dot{width:9px;height:9px;border-radius:50%;margin-top:5px;background:var(--brass)}
.dot[data-k="hot"]{background:var(--dl)} .dot[data-k="designed"]{background:var(--designed)}
.dot[data-k="implement"]{background:var(--stage-implement)} .dot[data-k="pr"]{background:var(--stage-pr)}
.dot[data-k="review"]{background:var(--stage-review)} .dot[data-k="triage"]{background:var(--stage-triage)}
.dot[data-k="fix"]{background:var(--stage-fix)} .dot[data-k="verify"]{background:var(--stage-verify)} .dot[data-k="merge"]{background:var(--stage-merge)}
.options{display:flex;flex-direction:column;background:var(--surface);border-top:3px solid var(--brass);border-radius:5px;padding:4px 14px;box-shadow:0 2px 6px color-mix(in srgb,var(--fg) 8%,transparent)}
.opt{display:grid;grid-template-columns:44px minmax(0,1fr);gap:2px 8px;padding:8px 6px;margin:0 -6px;width:calc(100% + 12px);border-radius:3px}
.opt+.opt{border-top:1px dotted var(--line)}
.opt:hover{background:var(--bg)}
label.opt,label.o{cursor:pointer} label.o:hover{border-color:var(--brass)}
.opt .k{font:700 16px/1.3 ui-monospace,monospace;color:var(--muted)}
.opt input,.o input{accent-color:var(--brass);margin:0 4px 0 0}
.opt .d{grid-column:2}
.tag{font-size:10px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--link);margin-left:6px}
.wi input[type=text]{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:3px;padding:4px 8px;min-width:0}
.wi button{font:inherit;font-size:13px;color:var(--link);background:var(--ref-bg);border:1px solid var(--brass);border-radius:3px;padding:3px 12px;cursor:pointer}
.d.wi input[type=text]{margin-right:6px}
.answered{border-left:3px solid var(--now);display:flex;flex-direction:column;gap:2px;margin-bottom:8px}
span.saved{font:11.5px ui-monospace,monospace;color:var(--now)}
.row.saved{border-left:3px solid var(--now)}
.default{border-left:3px solid var(--muted)}
.refs{display:grid;grid-template-columns:70px minmax(0,1fr) auto;gap:5px 10px;font-size:13px;align-items:baseline}
.refs .st{color:var(--muted);font-size:12px}
.fig{margin:0;padding:8px 10px 10px;background:none;border:0;border-radius:0}
.scroll{overflow-x:auto;background:none;border:0;border-radius:0}
.fig svg{display:block;max-width:100%;height:auto;width:auto;min-width:0}
figcaption{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-size:12px;color:var(--muted);border-top:1px dotted var(--line);padding-top:7px;margin-top:4px}
figcaption .chip.deployed{border:1.5px solid var(--deployed);color:var(--deployed)}
figcaption .chip.inflight{border:1.5px dashed var(--inflight);color:var(--inflight);background:var(--inflight-soft)}
figcaption .chip.designed{border:1.5px dotted var(--designed);color:var(--designed);background:var(--designed-soft)}
figcaption .chip.hot{border:2px solid var(--dl);color:var(--dl);background:none}
figcaption .chip.external{border:1.5px solid var(--ext);color:var(--ext-ink);border-radius:9px}
figcaption .chip.new{border:2px solid var(--stage-review);color:var(--fg);background:color-mix(in srgb,var(--stage-review) 14%,var(--surface))}
figcaption .chip.changed{border:2px solid var(--stage-implement);color:var(--fg);background:color-mix(in srgb,var(--stage-implement) 14%,var(--surface))}
figcaption .chip.removed{border:1.5px dashed var(--stage-fix);color:var(--fg);background:color-mix(in srgb,var(--stage-fix) 14%,var(--surface))}
figcaption .chip.drift{border:2.5px solid var(--stage-triage);color:var(--fg)}
figcaption .sum{font-family:ui-monospace,monospace}
pre.mermaid{margin:0;font:13px ui-monospace,monospace;background:none;border:0;padding:0;overflow:visible}
.graph{margin:0} .views{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px}
.view .scroll{max-height:480px;overflow:auto}
/* branch-graph's classDefs are shape only; its node classes take the legend's colours (mermaid's own rules are id-scoped). */
.view svg .node.added rect{fill:color-mix(in srgb,var(--stage-review) 14%,var(--surface))!important;stroke:var(--stage-review)!important}
.view svg .node.changed rect{fill:color-mix(in srgb,var(--stage-implement) 14%,var(--surface))!important;stroke:var(--stage-implement)!important}
.view svg .node.removed rect{fill:color-mix(in srgb,var(--stage-fix) 14%,var(--surface))!important;stroke:var(--stage-fix)!important}
svg text{font-family:"Alegreya Sans",system-ui,sans-serif;fill:var(--fg)}
svg .t{font-size:14px;font-weight:700} svg .t-ext{fill:var(--ext-ink)}
svg .tm{font-family:ui-monospace,monospace;font-size:10.5px;fill:var(--link)}
svg .tl{font-size:11.5px;fill:var(--muted)}
svg .tl.inflight{fill:var(--inflight)} svg .tl.designed{fill:var(--designed)} svg .tl.hot,svg .warn{fill:var(--dl);font-weight:700}
svg .halo{paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round}
svg .n{fill:var(--surface);stroke:var(--deployed);stroke-width:1.5}
svg .n-inflight{fill:var(--inflight-soft);stroke:var(--inflight);stroke-width:1.5;stroke-dasharray:6 4}
svg .n-designed{fill:var(--designed-soft);stroke:var(--designed);stroke-width:1.5;stroke-dasharray:2 3}
svg rect.ext{fill:var(--ext-soft);stroke:var(--ext);stroke-width:1.5}
svg .e{stroke:var(--deployed);stroke-width:1.5}
svg .e-inflight{stroke:var(--inflight);stroke-width:1.5;stroke-dasharray:6 4}
svg .e-designed{stroke:var(--designed);stroke-width:1.5;stroke-dasharray:2 3}
svg rect.hot,svg line.hot{stroke:var(--dl);stroke-width:2}
svg .ar-e{fill:var(--deployed)} svg .ar-e-inflight{fill:var(--inflight)} svg .ar-e-designed{fill:var(--designed)} svg .ar-hot{fill:var(--dl)}
.foot{font-size:12.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:8px}
/* decisions.html (Decisions.dc.html): a list, pending then completed. */
.decisions{display:flex;flex-direction:column;gap:16px;margin:0;padding:0;font-size:13.5px;line-height:1.45}
.decisions section{display:grid;gap:8px}
h2.pend{color:var(--link)}
.list{background:var(--surface);border:1px solid var(--line);border-top:3px solid var(--brass);border-radius:5px}
.list.fin{border-top-color:var(--now)}
.row{display:grid;align-items:baseline;gap:14px;padding:9px 14px}
.row+.row{border-top:1px dotted var(--line)}
.row.head{padding:6px 14px}
.row.pend{grid-template-columns:78px minmax(0,2.3fr) minmax(0,1.5fr) minmax(0,1fr) minmax(0,1.4fr)}
.row.done{grid-template-columns:78px minmax(0,2fr) minmax(0,1.2fr) minmax(0,1.3fr) minmax(0,1.2fr)}
.stack{display:flex;flex-direction:column;gap:2px;min-width:0;align-items:flex-start}
.holds{gap:3px}
.line{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}
.row .when,.age,.took,.page{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}
.row .when{font-size:12.5px} .age{font-size:11.5px;color:var(--dl)} .took{font-size:11.5px;color:var(--muted)}
.headline{font-weight:700;font-size:14.5px;color:var(--fg)} .done .headline{font-size:inherit}
.topic{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:0 5px}
.page{font-size:11px;color:var(--muted)}
.key{display:inline-block;font:700 11px/1.45 ui-monospace,monospace;border-radius:3px;padding:0 5px;margin-right:4px;border:1px solid var(--line);color:var(--muted)}
.done .key,.saved .key,.answered .key{color:var(--surface);background:var(--now);border-color:var(--now)}
.o{display:block;color:var(--fg);border:1px solid transparent;border-radius:3px;padding:1px 4px;margin:0 -5px}
.row .wi{display:flex;gap:4px;margin-top:2px;align-self:stretch}
.row .wi input[type=text]{flex:1 1 auto;padding:1px 6px;font-size:12.5px}
.row .wi button{padding:0 8px;font-size:12px}
.words{font-style:italic}
.holds>span,.fallback,.since,.none{font-size:12.5px;color:var(--muted)}
.decision-badge{font-size:11px;letter-spacing:.06em;padding:1px 6px;border-radius:3px;border:1px solid var(--line);color:var(--muted)}
.decision-badge.open,.decision-badge.blocked{border-color:var(--dl);color:var(--dl)}
.decision-answer{color:var(--muted)}
.decision-form{display:flex;flex-wrap:wrap;gap:6px}
.decision-form :is(input,button){font:13px/1.4 "Alegreya Sans","Gill Sans",system-ui,sans-serif}
.decision-form input[type=text]{flex:1 1 120px;min-width:0;padding:1px 6px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:3px}
.decision-form button{padding:0 8px;background:var(--ref-bg);color:var(--link);border:1px solid var(--brass);border-radius:3px;cursor:pointer}
.unread{border-left:3px dashed var(--dl)} .unread .why{color:var(--dl)} .unread .when{color:var(--muted)}
@media (max-width:760px){.row.pend,.row.done{grid-template-columns:minmax(0,1fr);gap:4px}.row.head{display:none}.row .stack:first-child{flex-direction:row;gap:8px}.unread .none{display:none}}
@media (max-width:900px){.cols{grid-template-columns:minmax(0,1fr)}}
@media (max-width:480px){header.top{flex-direction:column;align-items:flex-start;gap:6px}.refs{grid-template-columns:minmax(0,1fr)}}
""" + TAB_CSS + PROSE_CSS + "</style>\n"
HEAD = HEAD_OPEN + PAGE_CSS
