"""#231: CommonMark plus tables in prose, with plain metadata and offline installs."""
import re
import subprocess
import tempfile
import sys
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

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
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a' and 'a' in self.stack:
            self.nested_links = True
        if tag in ('h2', 'ul', 'pre', 'table', 'p') and 'p' in self.stack:
            self.blocks_in_p = True
        if tag not in ('img', 'input', 'br', 'hr', 'meta', 'link'):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


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

    def test_forge_references_do_not_rewrite_links_or_fences(self):
        html = dp._md('**#12** and `src/app.py:3` [#12](https://example.com/#12) <https://example.com/#12>', dp.Linker(GH))
        self.assertEqual(html.count('class="ref"'), 2)
        self.assertFalse(Structure(html).nested_links)
        block = page_common.markdown_blocks('```\n#12 src/app.py:3\n```', dp.Linker(GH).resolve)
        self.assertNotIn('<a', block)

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

    def test_issue_body_and_review_notes_render_blocks(self):
        self.assert_blocks(page(src=sources(MR, issue=replace(ISSUE, body=BLOCK))))
        thread = bs.ReviewThread(False, '**author**', BLOCK, NOW, 'https://example.com')
        self.assert_blocks(page(src=sources(replace(MR, threads=(thread,)))))
        self.assert_blocks(sp._note(sp.Note('src/a.py', 1, 'new', thread), NOW))

    def test_issue_prose_links_forge_references(self):
        from workspace import parse_coordinator
        from test_decision_page import BLOCK as CONFIG
        from test_issue_page import TODAY, today_tracker
        import issue_page
        cfg = parse_coordinator(CONFIG.format(board='- Backlog: GitHub; repo o/app'), today=TODAY)
        html = issue_page.render(109, [(TODAY, today_tracker())], sources(MR, issue=replace(ISSUE, body='See #12.')), NOW, cfg=cfg)
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

    def test_board_passes_markdown_and_forge_resolver_into_cards(self):
        import render_board
        from workspace import parse_coordinator
        from test_render_board import CLAUDE_MD, LANED_TRACKER, NOW
        cfg = parse_coordinator(CLAUDE_MD.replace('- Calendar tool:', '- Backlog: GitHub; repo o/app\n- Calendar tool:'), today=NOW.date())
        html = render_board.render(LANED_TRACKER.replace('Write eval README', '**Write** *README* #12'), cfg, NOW)
        self.assertIn('<b>Write</b> <em>README</em>', html)
        self.assertIn('href="https://github.com/o/app/issues/12"', html)
        self.assertFalse(Structure(html).nested_links)

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


if __name__ == '__main__':
    unittest.main()
