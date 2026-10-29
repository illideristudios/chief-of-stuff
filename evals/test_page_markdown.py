"""#231: CommonMark plus tables in prose, with plain metadata and offline installs."""
import copy
import os
import re
import subprocess
import tempfile
import sys
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'evals'))
import columns
import decision_page as dp
import page_common
import panels
import source_page as sp
from test_decision_page import decision, GH
from test_issue_page import ISSUE, MR, NOW, page, sources
import board_sources as bs

BLOCK = '## Report\n\n**Passed** and *reviewed*.\n\n- first\n  - nested\n\n```python\nprint("<safe>") #12\n```\n\n| Check | Result |\n| --- | --- |\n| build | pass |\n'


class Structure(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.nested_links = False
        self.blocks_in_p = False
        self.nodes = []
        self.parents = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), parent=self.parents[-1] if self.parents else None, text='')
        self.nodes.append(node)
        if tag == 'a' and 'a' in self.stack:
            self.nested_links = True
        if tag in ('h2', 'ul', 'pre', 'table', 'p') and 'p' in self.stack:
            self.blocks_in_p = True
        if tag not in ('img', 'input', 'br', 'hr', 'meta', 'link'):
            self.stack.append(tag)
            self.parents.append(node)

    def handle_data(self, data):
        for node in self.parents:
            node['text'] += data

    def by_class(self, cls):
        return [n for n in self.nodes if cls in n['attrs'].get('class', '').split()]

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]
            self.parents = self.parents[:len(self.stack)]


class MarkdownTest(unittest.TestCase):
    def assert_blocks(self, html):
        for mark in ('<h2>Report</h2>', '<ul>', '<pre><code class="language-python">', '<table>', '<em>reviewed</em>'):
            self.assertIn(mark, html)
        self.assertRegex(html, r'<ul>\s*<li>first\s*<ul>')
        self.assertIn('&lt;safe&gt;', html)
        self.assertFalse(Structure(html).blocks_in_p)

    def test_shared_block_renderer(self):
        self.assert_blocks(page_common.markdown_blocks(BLOCK))

    def test_inline_has_no_block_elements(self):
        html = page_common.markdown_inline('## title\n\n- **bold** and *italic*, ``a ` b``, [link](https://example.com/a?q=1&b=2)')
        self.assertNotRegex(html, r'<(?:p|h\d|ul|li|pre|table)\b')
        self.assertIn('<em>italic</em>', html)
        self.assertIn('<code>a ` b</code>', html)
        self.assertIn('target="_blank" rel="noopener"', html)

    def test_commonmark_entities_escapes_and_reference_links(self):
        html = page_common.markdown_blocks('\\*literal* &amp; **bold *inside***\n\n[docs][d]\n\n[d]: https://example.com')
        self.assertIn('*literal* &amp;', html)
        self.assertRegex(html, r'<(?:strong|b)>bold <em>inside</em></(?:strong|b)>')
        self.assertIn('href="https://example.com"', html)

    def test_raw_html_and_unsafe_links_are_escaped(self):
        for render in (page_common.markdown_inline, page_common.markdown_blocks):
            html = render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)> [bad](javascript:alert%281%29)')
            self.assertNotIn('<script>', html)
            self.assertNotIn('<img ', html)
            self.assertNotIn('href="javascript:', html)
            self.assertIn('&lt;script&gt;', html)

    def test_a_bare_html_comment_stays_escaped_in_markdown_blocks(self):
        html = page_common.markdown_blocks('<!-- codex-code-review-finding:v1 -->')
        self.assertIn('&lt;!--', html)

    def test_forge_references_do_not_rewrite_links_or_fences(self):
        html = dp._md('**#12** and `src/app.py:3` [#12](https://example.com/#12) <https://example.com/#12>', dp.Linker(GH))
        self.assertEqual(html.count('class="ref"'), 2)
        self.assertFalse(Structure(html).nested_links)
        block = page_common.markdown_blocks('```\n#12 src/app.py:3\n```', dp.Linker(GH).resolve)
        self.assertNotIn('<a', block)

    def test_angle_brackets_and_literal_backticks_do_not_hide_references(self):
        for render in (page_common.markdown_inline, page_common.markdown_blocks):
            for text in ('<#12>', 'a < b and #12 > 0', '``literal ` and #12`` #13'):
                with self.subTest(render=render.__name__, text=text):
                    html = render(text, dp.Linker(GH).resolve)
                    number = 13 if 'literal' in text else 12
                    self.assertIn(f'href="https://github.com/o/app/issues/{number}"', html)
                    if number == 13:
                        self.assertNotIn('issues/12', html)

    def test_escaped_and_entity_references_stay_literal(self):
        for render in (page_common.markdown_inline, page_common.markdown_blocks):
            html = render(r'\#12 &#35;13 &num;14 \!15 #16', dp.Linker(GH).resolve)
            self.assertEqual(html.count('class="ref"'), 1)
            self.assertIn('issues/16', html)
            self.assertIn('#12 #13 #14 !15', html)

    def test_concurrent_cold_renders_match_sequential_results(self):
        from _vendor.markdown_it.ruler import Ruler
        cold = copy.copy(page_common._MARKDOWN)
        for name in ('core', 'block', 'inline'):
            parser = copy.copy(getattr(cold, name))
            parser.ruler = copy.deepcopy(parser.ruler)
            if name == 'inline':
                parser.ruler2 = copy.deepcopy(parser.ruler2)
            setattr(cold, name, parser)
        compiling, release, second_started = threading.Event(), threading.Event(), threading.Event()
        compile_rules = Ruler.__compile__

        def paused_compile(ruler):
            if ruler is cold.core.ruler:
                # Reproduce the parser's published-but-incomplete cache deterministically.
                ruler.__cache__ = {}
                compiling.set()
                if not release.wait(5):
                    raise AssertionError('compiler was never released')
            compile_rules(ruler)

        def second_render():
            second_started.set()
            return page_common.markdown_inline('**other** #12', dp.Linker(GH).resolve)

        with mock.patch.object(page_common, '_MARKDOWN', cold):
            expected = (page_common.markdown_blocks(BLOCK), second_render())
            for ruler in (cold.core.ruler, cold.block.ruler, cold.inline.ruler, cold.inline.ruler2):
                ruler.__cache__ = None
            second_started.clear()
            with mock.patch.object(Ruler, '__compile__', paused_compile), ThreadPoolExecutor(2) as pool:
                first = pool.submit(page_common.markdown_blocks, BLOCK)
                try:
                    self.assertTrue(compiling.wait(5))
                    second = pool.submit(second_render)
                    self.assertTrue(second_started.wait(5))
                    try:
                        second.result(timeout=0.1)
                    except TimeoutError:
                        pass  # Serialized rendering waits for the first compilation.
                finally:
                    release.set()
                self.assertEqual((first.result(5), second.result(5)), expected)

    def test_vendored_packages_load_without_site_packages(self):
        proc = subprocess.run([sys.executable, '-S', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); from page_common import markdown_blocks; '
            'from _vendor.markdown_it import __version__; assert __version__ == "4.2.0"; '
            'assert "<table>" in markdown_blocks("| a |\\n| --- |\\n| b |")', str(ROOT / 'scripts')],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_pinned_install_includes_parser_and_licenses(self):
        import start_coordinator
        with tempfile.TemporaryDirectory() as tmp:
            release = start_coordinator.install(ROOT, Path(tmp))
            for name in ('markdown_it/LICENSE', 'markdown_it/LICENSE.markdown-it', 'mdurl/LICENSE'):
                self.assertIn('Permission is hereby granted', (release / 'scripts' / '_vendor' / name).read_text())
            proc = subprocess.run([sys.executable, '-S', '-c',
                'import sys; sys.path.insert(0, sys.argv[1]); from page_common import markdown_blocks; '
                'assert "<h2>Report</h2>" in markdown_blocks("## Report")', str(release / 'scripts')],
                cwd=tmp, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_card_markdown_links_do_not_nest_in_navigation(self):
        html = columns.render([columns.Column('Build', (columns.Card('[docs](https://example.com)', 'open', href='/issues/12'),))], inline=page_common.markdown_inline)
        self.assertFalse(Structure(html).nested_links)
        self.assertIn('href="https://example.com"', html)

    def test_decision_sections_render_reports_as_blocks(self):
        self.assert_blocks(dp.render(decision(sections=[{'heading': 'Results', 'text': BLOCK}]), '2026-10-16'))

    def test_decision_long_prose_fields_render_blocks(self):
        for field in ('why', 'default', 'yes', 'ask'):
            with self.subTest(field=field):
                self.assert_blocks(dp.render(decision(**{field: BLOCK}), '2026-10-16'))
        self.assert_blocks(dp.render(decision(sides=[{'label':'Plan','status':'open','text':BLOCK}]), '2026-10-16'))

    def test_decision_question_prose_stays_prominent(self):
        for yes in ('', 'A yes does this.'):
            html = dp.render(decision(ask='Should we proceed?', yes=yes), '2026-10-16')
            dom = Structure(html)
            ask, = dom.by_class('ask')
            divs = [n for n in dom.nodes if n['parent'] is ask and n['tag'] == 'div']
            # Match the actual selector in the rendered page, including :first-of-type.
            self.assertIn('.ask>.prose:first-of-type p{color:var(--fg);font-size:16px}', html)
            self.assertIn('prose', divs[0]['attrs'].get('class', '').split())
            prominent = [n for n in dom.nodes if n['tag'] == 'p' and n['parent'] is divs[0]]
            self.assertEqual([n['text'] for n in prominent], ['Should we proceed?'])
            self.assertNotIn(yes, [n['text'] for n in prominent])

    def test_html_comment_block_ends_at_bang_close(self):
        from _vendor.markdown_it import MarkdownIt
        html = MarkdownIt('commonmark').render('<!-- note --!>\n\nafter\n')
        self.assertIn('<p>after</p>', html)

    def test_issue_body_and_review_notes_render_blocks(self):
        self.assert_blocks(page(src=sources(MR, issue=replace(ISSUE, body=BLOCK))))
        thread = bs.ReviewThread(False, '**author**', BLOCK, NOW, 'https://example.com')
        self.assert_blocks(page(src=sources(replace(MR, threads=(thread,)))))
        self.assert_blocks(sp._note(sp.Note('src/a.py', 1, 'new', thread), NOW))

    def test_detail_pages_include_prose_styles_in_their_rendered_head(self):
        from fragment import PROSE_CSS
        from test_issue_page import TODAY, today_tracker
        htmls = (page(src=sources(MR, issue=replace(ISSUE, body=BLOCK))),
                 sp.render(109, [(TODAY, today_tracker())], sources(MR), NOW))
        for html in htmls:
            self.assertIn(PROSE_CSS, html.split('</head>')[0])

    def test_diff_note_spacing_only_applies_to_the_outer_cell(self):
        thread = bs.ReviewThread(False, 'author', BLOCK, NOW, 'https://example.com')
        f = sp.File('src/a.py', 'CHANGED')
        f.rows = [('add', '', 1, 'x')]
        note = sp.Note('src/a.py', 1, 'new', thread)
        self.assertIn('tr.note>td{padding:4px 14px 8px 100px', sp.CSS)
        self.assertNotRegex(sp.CSS, r'tr\.note\s+td\s*\{')
        for html in (sp._rows(f, [note], NOW)[0], sp._split(f, [note], NOW)):
            cells = [n for n in Structure(html).nodes if n['tag'] == 'td']
            outer = [n for n in cells if n['parent']['attrs'].get('class') == 'note']
            nested = [n for n in cells if n['text'] in ('build', 'pass')]
            self.assertEqual(len(outer), 1)
            self.assertEqual(len(nested), 2)
            # `tr.note>td` reaches only the cell that holds the note, never a cell of the table inside it.
            self.assertTrue(all(n['parent']['attrs'].get('class') != 'note' for n in nested))

    def test_references_survive_escapes_and_entities_in_the_same_run(self):
        resolve = lambda m: ('issue', m[0], '/i/' + m['num']) if m['num'] else None
        cases = {
            'https://example.com/?a=1&amp;b=2': 'href="https://example.com/?a=1&amp;b=2"',
            'src/my\\_file.py:3': 'src/my_file.py:3</a>',
        }
        for text, expect in cases.items():
            with self.subTest(text=text):
                self.assertIn(expect, page_common.markdown_inline(text, lambda m: ('file', m[0], 'blob/' + m[0]) if m['path'] else None))
        for escaped in ('\\#12', '&#35;12'):
            with self.subTest(escaped=escaped):
                self.assertNotIn('<a', page_common.markdown_inline(escaped, resolve))

    def test_issue_prose_links_forge_references(self):
        from workspace import parse_coordinator
        from test_decision_page import BLOCK as CONFIG
        from test_issue_page import TODAY, today_tracker
        import issue_page
        cfg = parse_coordinator(CONFIG.format(board='- Backlog: GitHub; repo o/app'), today=TODAY)
        issue = replace(ISSUE, url='https://github.com/o/app/issues/109', body='See #12.')
        src = replace(sources(MR, issue=issue), home=bs.backlog.GitHubBacklog('o/app'))
        html = issue_page.render(109, [(TODAY, today_tracker())], src, NOW, cfg=cfg)
        self.assertIn('href="https://github.com/o/app/issues/12"', html)

    def test_cards_and_panel_prose_use_inline_markdown(self):
        html = columns.render([columns.Column('**label**', (columns.Card('**Title** *detail* `code`', 'open', owner='**owner**', state='**state**'),))], inline=page_common.markdown_inline)
        self.assertIn('<em>detail</em>', html)
        self.assertIn('<code>code</code>', html)
        for raw in ('**label**', '**owner**', '**state**'):
            self.assertIn(raw, html)
        html = panels.panels([panels.Panel('x', '**label**', 'open', 2, (panels.Row('name', '**passed** *check*'),))], inline=page_common.markdown_inline)
        self.assertIn('<em>check</em>', html)
        self.assertIn('**label**', html)

    def test_board_card_names_keep_navigation_for_references_and_links(self):
        import render_board
        from workspace import parse_coordinator
        from test_render_board import CLAUDE_MD, LANED_TRACKER, NOW
        cfg = parse_coordinator(CLAUDE_MD.replace('- Calendar tool:', '- Backlog: GitHub; repo o/app\n- Calendar tool:'), today=NOW.date())
        tracker = LANED_TRACKER.replace('| implement |  |', '| implement | #12 |')
        for suffix in ('#12', '(#12)', 'https://example.com', '[docs](https://example.com)', '`src/app.py:3`'):
            with self.subTest(suffix=suffix):
                html = render_board.render(tracker.replace('Write eval README', '**Write** *README* ' + suffix), cfg, NOW)
                dom = Structure(html)
                name, = dom.by_class('columns-card-name')
                self.assertEqual(name['tag'], 'a')
                self.assertEqual(name['attrs'].get('href'), '/issues/12')
                self.assertIn('<b>Write</b> <em>README</em>', html)
                self.assertIn('<div class="columns-refs"><a href="/issues/12"', html)
                self.assertFalse(dom.nested_links)

    def test_merged_card_names_keep_the_change_link(self):
        import render_board
        from workspace import parse_coordinator
        from test_render_board import CLAUDE_MD
        merged = replace(MR, title='**Merged** change (#12)', state='merged', merged_at=NOW)
        src = sources(merged)
        cfg = replace(parse_coordinator(CLAUDE_MD, today=NOW.date()), backlog=src.home)
        html = render_board.render('', cfg, NOW, sources=src)
        name, = Structure(html).by_class('columns-card-name')
        self.assertEqual(name['tag'], 'a')
        self.assertEqual(name['attrs'].get('href'), '/issues/109')

    def test_board_card_without_navigation_keeps_its_markdown_link(self):
        import render_board
        from workspace import parse_coordinator
        from test_render_board import CLAUDE_MD, LANED_TRACKER, NOW
        html = render_board.render(LANED_TRACKER.replace('Write eval README', '[docs](https://example.com)'),
                                   parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW)
        name, = Structure(html).by_class('columns-card-name')
        self.assertEqual(name['tag'], 'div')
        self.assertIn('<a href="https://example.com"', html)

    def test_checklist_and_log_prose_use_inline_markdown(self):
        import issue_page
        html = issue_page._acceptance(replace(ISSUE, body='## Acceptance\n- [ ] **pass** *review* `cmd`'))
        self.assertIn('<em>review</em>', html)
        d = decision(timeline=[{'when':'now','text':'## **done** *reviewed*'}])
        html = dp.render(d, '2026-10-16')
        self.assertIn('## <b>done</b> <em>reviewed</em>', html)

    def test_plain_decision_names_and_labels(self):
        html = dp.render(decision(headline='**name**', references=[{'kind':'issue', 'label':'**label**', 'value':'#12'}]), '2026-10-16', GH)
        self.assertIn('<h1>**name**</h1>', html)
        self.assertIn('**label**', html)

    def test_local_reference_reads_fail_softly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('def start():\n    pass\n')
            for error in (PermissionError('denied'), FileNotFoundError('removed')):
                with self.subTest(error=type(error).__name__):
                    linker = dp.Linker(GH, root)
                    with mock.patch.object(Path, 'resolve', side_effect=error):
                        self.assertEqual(linker._find('app.py', None, 'start'), '')
                    # Missing metadata/open permissions must not stop prose rendering either.
                    linker = dp.Linker(GH, root)
                    with mock.patch('os.open', side_effect=error):
                        self.assertNotIn('#L1', dp._md('app.py:start', linker))

    def test_local_reference_reads_are_bounded_and_cached_per_linker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'app.py'
            path.write_text('def start():\n    pass\n' + 'x' * (2 * 1024 * 1024))
            self.assertEqual(dp.Linker(GH, root)._find('app.py', None, 'start'), '')
            path.write_text('def start():\n    pass\n\ndef stop():\n    pass\n')
            linker = dp.Linker(GH, root)
            self.assertEqual(linker._find('app.py', None, 'start'), '#L1')
            path.unlink()
            self.assertEqual(linker._find('app.py', None, 'stop'), '#L4')
            self.assertEqual(dp.Linker(GH, root)._find('app.py', None, 'stop'), '')

    def test_local_reference_never_reads_a_fifo_or_a_path_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            (root.parent / 'outside.py').write_text('def start():\n    pass\n')
            (root / 'link.py').symlink_to(root.parent / 'outside.py')
            os.mkfifo(root / 'pipe.py')
            linker = dp.Linker(GH, root)
            self.assertEqual(linker._find('link.py', None, 'start'), '')
            self.assertEqual(linker._find('pipe.py', None, 'start'), '')

    def test_saved_numeric_words_render_as_text(self):
        d = dp.parse(decision(answer={'words': 42, 'at': NOW.isoformat()}))
        self.assertIn('“42”', dp.render(d, NOW.date().isoformat()))


if __name__ == '__main__':
    unittest.main()
