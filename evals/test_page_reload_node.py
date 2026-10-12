"""The page reload check, run in node against a stub page (#337: "four mutants of its reload and typing logic survive
the suite"). test_pages.py pins the snippet's text; this runs its branches.

node is the runtime (Zach: "node's fine"), used with built-ins only: a hand-written stub of the few DOM calls the
snippet makes, a fake clock (the test calls the interval's function) and a fake fetch. Without node the node tests
skip with a reason naming node and #337; the skip reason itself is tested in a class that never skips.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import page_reload  # noqa: E402  (test_pages.py pins that every served page carries the shared reload check)

NODE_TIMEOUT = 10  # seconds; a hung node must not stall CI


def skip_reason(which=shutil.which) -> str:
    """"" when node is on PATH, else why the node tests cannot run. Never empty when node is missing."""
    if which("node"):
        return ""
    return "node is not on PATH: the page reload check is not run in a JS runtime, so its branches are ungraded (#337)"


# The stub page and fake clock. Reads {snippet, scenarios} as JSON on stdin, prints one {reloads, notices} per scenario
# as JSON (one node start for all of them: a start is ~0.1 s). A result carries `badFetch` only when the poll's request
# was wrong, which fails its case.
DRIVER = r"""
const vm = require('vm');
const {snippet, scenarios} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const flush = () => new Promise(r => setImmediate(r));
async function run(scenario) {
let header = scenario.header;
let controls = (scenario.controls || []).slice();
let reloads = 0, intervals = [], listeners = {}, notices = [];
const control = c => {
  if ((c.tag || '').toUpperCase() === 'SELECT') {
    const options = (c.options || []).map(o => Object.assign({selected: false, defaultSelected: false, disabled: false}, o));
    return {tag: 'SELECT', tagName: 'SELECT', multiple: !!c.multiple, size: c.size || 0, options,
            value: '', defaultValue: '', checked: false, defaultChecked: false,
            inForm: c.inForm !== false, formAttr: !!c.formAttr};
  }
  return Object.assign({tag: c.tag, tagName: (c.tag || '').toUpperCase(), value: '', defaultValue: '',
                        checked: false, defaultChecked: false, selected: false, defaultSelected: false,
                        inForm: true, formAttr: false}, c);
};
function el(tag) {
  const n = {tagName: tag.toUpperCase(), style: {}, children: [], _text: '', setAttribute() {},
             appendChild(c) { this.children.push(c); }};
  Object.defineProperty(n, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v); this.children = []; },  // #388: a write clears children, as in a browser
  });
  return n;
}
const document = {
  lastModified: scenario.lastModified,
  activeElement: scenario.active || null,
  body: {appendChild: n => notices.push(n)},
  getElementById: id => notices.find(n => n.id === id) || null,
  createElement: el,
  addEventListener: (type, f) => { (listeners[type] = listeners[type] || []).push(f); },
  // `form`: one form whose .elements are in-form controls and those tied by the form attribute (#388).
  // `form input` / `form option` (older snippet): a part is [form] tag; `form` needs the control inside one.
  querySelectorAll: sel => {
    if (sel === 'form') {
      const members = controls.map(control).filter(c => c.inForm || c.formAttr);
      const nested = controls.map(control).filter(c => c.inForm);  // tree descendants, not form= (#388 mutant)
      return [{elements: members, querySelectorAll: q => nested.filter(c =>
        q.split(',').some(t => (c.tag || '').toLowerCase() === t.trim().toLowerCase()))}];
    }
    const flat = [];
    for (const c of controls.map(control)) {
      if (c.tagName === 'SELECT') {
        for (const o of c.options) flat.push(Object.assign({tag: 'option', tagName: 'OPTION', inForm: c.inForm}, o));
      } else flat.push(c);
    }
    return flat.filter(c => sel.split(',').some(part => {
      const words = part.trim().split(/\s+/), tag = words.pop();
      return (c.tag || '').toLowerCase() === tag.toLowerCase() && (words[0] !== 'form' || c.inForm);
    }));
  },
};
// The poll must be a HEAD of this page, uncached; any other request is recorded and answered with no header.
// The header value is captured at request time so a load-time HEAD is not rewritten by a later step (#332).
const badFetch = [];
const fetch = (url, opts) => {
  if (url !== location.pathname || !opts || opts.method !== 'HEAD' || opts.cache !== 'no-store') {
    badFetch.push([url, opts]);
    return Promise.resolve({headers: {get: () => null}});
  }
  const at = header;
  return at === 'fail' ? Promise.reject(new Error('offline'))
    : Promise.resolve({headers: {get: name => name === 'Last-Modified' ? at : null}});
};
const location = {pathname: '/x.html', reload() { reloads++; }};
const sandbox = {document, location, fetch, setInterval: f => intervals.push(f), isNaN, Date};
  new vm.Script(snippet.replace(/^\s*<script>|<\/script>\s*$/g, '')).runInNewContext(sandbox, {timeout: 2000});
  await flush();  // finish the load-time HEAD before steps run; the GET supplies the baseline
  for (const step of scenario.steps) {
    if ('header' in step) header = step.header;
    if ('active' in step) document.activeElement = step.active;
    if ('controls' in step) controls = step.controls;
    if (step.tick) for (let i = 0; i < step.tick; i++) { intervals.forEach(f => f()); await flush(); }
    if (step.visible) { (listeners.visibilitychange || []).forEach(f => f()); await flush(); }
    if (step.click && notices[0] && notices[0].children[0]) notices[0].children[0].onclick();
  }
  return Object.assign({reloads, notices: notices.length}, badFetch.length ? {badFetch} : {});
}
(async () => {
  const out = [];
  for (const s of scenarios) out.push(await run(s));
  console.log(JSON.stringify(out));
})();
"""

SAME = "Tue, 06 Oct 2026 12:00:00 GMT"
LATER = "Tue, 06 Oct 2026 12:00:05 GMT"
EPOCH = "Thu, 01 Jan 1970 00:00:00 GMT"
# An hour behind SAME when Date.parse'd in TZ=UTC: the DST fall-back case (#332) if the snippet still compared
# document.lastModified to the header.
DOC_TIME = "10/06/2026 12:00:00"
DOC_HOUR_OFF = "10/06/2026 11:00:00"


class NodeFailed(Exception):
    """node did not run the stub page to a result: a non-zero exit, a timeout or output that is not the result."""


def run_node(snippet: str, scenarios: list[dict], timeout: float = NODE_TIMEOUT, driver: str = DRIVER) -> list[dict]:
    """One result per scenario, from one node run of `snippet` against a fresh stub page each."""
    scenarios = [{"lastModified": DOC_TIME, "header": SAME, **s} for s in scenarios]
    try:
        done = subprocess.run(["node", "-e", driver], input=json.dumps({"snippet": snippet, "scenarios": scenarios}),
                              capture_output=True, text=True, timeout=timeout, env={**os.environ, "TZ": "UTC"})
    except subprocess.TimeoutExpired:
        raise NodeFailed(f"node did not finish in {timeout}s") from None
    if done.returncode:
        raise NodeFailed(f"node exited {done.returncode}: {done.stderr.strip()[:500]}")
    try:
        got = json.loads(done.stdout)
    except ValueError:
        raise NodeFailed(f"node printed no result: {done.stdout[:200]!r} {done.stderr.strip()[:200]}") from None
    if not isinstance(got, list) or len(got) != len(scenarios):
        raise NodeFailed(f"node printed {got!r:.200} for {len(scenarios)} scenarios")
    return got


# The snippet swallows every throw on its poll path with this catch; a throw there (a stub method missing, a mutant
# that crashes) would look like a wrong count. In a throwaway copy it rethrows, which node reports as a non-zero exit.
QUIET_CATCH = ".catch(function(){})"
LOUD_CATCH = ".catch(function(e){if(!e||e.message!=='offline')throw e;})"


def loud(snippet: str) -> str:
    assert snippet.count(QUIET_CATCH) == 1, "the snippet's swallowing catch is not there exactly once to make loud"
    return snippet.replace(QUIET_CATCH, LOUD_CATCH)


def grade(snippet: str) -> list[dict]:
    """One result per case in SCENARIOS, run with the poll's throws made loud."""
    return run_node(loud(snippet), [scenario for scenario, _ in SCENARIOS.values()])


def typed(**kw): return {"tag": "input", "value": "x", "defaultValue": "", **kw}
def picked_radio(**kw): return {"tag": "input", "checked": True, "defaultChecked": False, **kw}


def select_control(options, multiple=False, **kw):
    return {"tag": "SELECT", "multiple": multiple, "options": options, **kw}


def picked_option(**kw):
    """A select whose current option is not its default."""
    return select_control([{"selected": False, "defaultSelected": True},
                           {"selected": True, "defaultSelected": False}], **kw)


def untouched_select(**kw):
    """A single select with no selected attribute: the browser selects the first option (#339)."""
    return select_control([{"selected": True, "defaultSelected": False},
                           {"selected": False, "defaultSelected": False}], **kw)


def focused(tag, **kw): return {"tagName": tag, "isContentEditable": False, **kw}


def changed(*steps, **scenario):
    """A page whose Last-Modified moves to LATER, then `steps`."""
    return {"steps": [{"header": LATER}, *steps], **scenario}


# name -> (scenario, expected result). reloads is a count: the snippet's reload comparison, typing guard, notice.
RELOAD, NOTICE, NOTHING = {"reloads": 1, "notices": 0}, {"reloads": 0, "notices": 1}, {"reloads": 0, "notices": 0}
SCENARIOS = {
    "unchanged time, three polls": ({"steps": [{"tick": 3}]}, NOTHING),
    "changed time reloads once": (changed({"tick": 1}), RELOAD),
    "visibilitychange polls too": (changed({"visible": 1}), RELOAD),
    "no Last-Modified header": ({"header": None, "steps": [{"tick": 1}]}, NOTHING),
    "a header that disappears is not a change": ({"steps": [{"header": None}, {"tick": 1}]}, NOTHING),
    "an epoch Last-Modified is a time, not an absent one": ({"steps": [{"header": EPOCH}, {"tick": 1}]}, RELOAD),
    "a failed fetch is ignored": ({"steps": [{"header": "fail"}, {"tick": 2}]}, NOTHING),
    "first HEAD fails, then the page changes": (changed({"tick": 1}, header="fail"), RELOAD),
    "page changed between GET and first HEAD": ({"header": LATER, "steps": []}, RELOAD),
    # #332: document.lastModified an hour off the header must not reload while the header is unchanged.
    "lastModified disagrees with header by an hour, header unchanged": (
        {"lastModified": DOC_HOUR_OFF, "steps": [{"tick": 3}]}, NOTHING),
    "lastModified disagrees with header by an hour, header changes": (
        {"lastModified": DOC_HOUR_OFF, "steps": [{"header": LATER}, {"tick": 1}]}, RELOAD),
    "typing without a change does not notice": ({"controls": [typed()], "steps": [{"tick": 2}]}, NOTHING),
    "typed text": (changed({"tick": 1}, controls=[typed()]), NOTICE),
    "typed text, changed after it is typed": ({"steps": [{"controls": [typed()]}, {"header": LATER}, {"tick": 1}]}, NOTICE),
    "typed textarea": (changed({"tick": 1}, controls=[typed(tag="textarea")]), NOTICE),
    "a picked radio or checkbox in a form": (changed({"tick": 1}, controls=[picked_radio()]), NOTICE),
    "a chosen option in a form": (changed({"tick": 1}, controls=[picked_option()]), NOTICE),
    # #339: a plain <select> with no selected option is not typed input.
    "an untouched select is a clean form": (changed({"tick": 1}, controls=[untouched_select()]), RELOAD),
    "an untouched list box with no selection is a clean form": (
        changed({"tick": 1}, controls=[select_control([{}, {}], size=2)]), RELOAD),
    "focused input": (changed({"tick": 1}, active=focused("INPUT")), NOTICE),
    "focused textarea": (changed({"tick": 1}, active=focused("TEXTAREA")), NOTICE),
    "focused select": (changed({"tick": 1}, active=focused("SELECT")), NOTICE),
    "focused rich text": (changed({"tick": 1}, active=focused("DIV", isContentEditable=True)), NOTICE),
    "focused something else": (changed({"tick": 1}, active=focused("BODY")), RELOAD),
    # form.elements includes buttons, which have no defaultValue: a form with one is still clean.
    "a button in a clean form": (changed({"tick": 1}, controls=[{"tag": "button", "value": "go", "defaultValue": None}]), RELOAD),
    "a clean form": (changed({"tick": 1}, controls=[typed(value="", defaultValue=""), picked_radio(checked=False)]), RELOAD),
    "a radio outside any form": (changed({"tick": 1}, controls=[picked_radio(inForm=False), typed(inForm=False),
                                                                 typed(tag="textarea", inForm=False),
                                                                 picked_option(inForm=False)]), RELOAD),
    # #388: a control tied by the form attribute sits outside the <form> in the tree but is still form state.
    "typed text tied by the form attribute": (changed({"tick": 1}, controls=[typed(inForm=False, formAttr=True)]), NOTICE),
    "the notice shows once across polls": (changed({"tick": 4}, controls=[typed()]), NOTICE),
    "its button reloads": (changed({"tick": 2}, {"click": 1}, controls=[typed()]), {"reloads": 1, "notices": 1}),
}


def failures(snippet: str) -> list[str]:
    """The scenarios `snippet` does not satisfy."""
    return [name for name, result, (_, want) in zip(SCENARIOS, grade(snippet), SCENARIOS.values(), strict=True)
            if result != want]


# (name, text in the snippet, its mutation). Each is a text substitution made in a throwaway copy, never committed.
MUTANTS = [
    # the four named in #337 and the review of the PR that added the check
    ("reload comparison inverted", "t!==seen", "t===seen"),
    ("always reload", "t!=null&&t!==seen", "t!=null"),
    ("typing() always true", "if(typing())notice()", "if(true)notice()"),
    ("notice not once-only", "if(document.getElementById('page-changed'))return;", ""),
    # the rest of the branches its guards pin
    ("typing() never true", "if(typing())notice()", "if(false)notice()"),
    ("a null header is a change", "t!=null&&t!==seen", "t!==seen"),
    ("focus is not typing", "a&&(/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)||a.isContentEditable)||", ""),
    ("SELECT is not typing", "INPUT|TEXTAREA|SELECT", "INPUT|TEXTAREA"),
    ("rich text is not typing", "||a.isContentEditable", ""),
    ("edited text is not unsaved", "e.value!==e.defaultValue||", ""),
    ("a picked radio is not unsaved", "e.checked!==e.defaultChecked", "false"),
    ("a chosen option is not unsaved", "return selectDirty(e);", "return false;"),
    ("an untouched select counts as typed", "if(!def.length&&!s.multiple&&s.size<=1)", "if(false)"),
    ("a list box defaults to its first option", "&&s.size<=1", ""),
    ("the first HEAD sets the baseline", "var t=r.headers.get('Last-Modified');",
     "var t=r.headers.get('Last-Modified');if(!poll.ready){seen=t;poll.ready=true;return;}"),
    ("a button counts as edited text", "if(!/^(INPUT|TEXTAREA)$/.test(e.tagName)) return false;", ""),
    ("form-attribute controls are missed", "f.elements", "f.querySelectorAll('input,textarea,select')"),
    ("the button does not reload", "b.onclick=function(){location.reload();}", "b.onclick=function(){}"),
    # #388: append then textContent — in a browser that clears the button; the stub must model that.
    ("textContent after append clears the button",
     "n.textContent='This page changed. ';\n    var b=document.createElement('button');b.textContent='Reload';b.onclick=function(){location.reload();};\n    n.appendChild(b);document.body.appendChild(n);",
     "var b=document.createElement('button');b.textContent='Reload';b.onclick=function(){location.reload();};\n    n.appendChild(b);n.textContent='This page changed. ';document.body.appendChild(n);"),
    # the poll's request, which the stub's fetch checks
    ("the poll is not a HEAD", "method:'HEAD'", "method:'GET'"),
    ("the poll may be cached", ",cache:'no-store'", ""),
    ("the poll has no options", "{method:'HEAD',cache:'no-store'}", "{}"),
    ("the poll asks for another path", "fetch(location.pathname,", "fetch('/elsewhere',"),
    ("the poll asks for no path", "fetch(location.pathname,", "fetch("),
]


@unittest.skipIf(skip_reason(), skip_reason())
class ReloadSnippetInNodeTest(unittest.TestCase):
    """The snippet every served page carries, run in node against the stub page."""

    snippet = page_reload.inject(b"", SAME).decode()

    def test_the_served_snippet_does_what_the_cases_say(self):
        for (name, (_, want)), result in zip(SCENARIOS.items(), grade(self.snippet), strict=True):
            with self.subTest(name):
                self.assertEqual(result, want)

    def test_every_mutant_dies(self):
        for name, old, new in MUTANTS:
            with self.subTest(name):
                self.assertEqual(self.snippet.count(old), 1, "the text to mutate is not in the snippet exactly once")
                self.assertNotEqual(failures(self.snippet.replace(old, new)), [], "the mutant survives every case")

    def test_node_failures_are_errors_not_results(self):
        with self.assertRaisesRegex(NodeFailed, "exited 1"):
            run_node("throw new Error('boom')", [{"steps": []}])
        with self.assertRaisesRegex(NodeFailed, "did not finish"):
            run_node("for(;;){}", [{"steps": []}], timeout=1, driver=DRIVER.replace("timeout: 2000", "timeout: 60000"))
        with self.assertRaisesRegex(NodeFailed, "no result"):
            run_node("", [{"steps": []}], driver="console.log('not json')")
        with self.assertRaisesRegex(NodeFailed, "for 2 scenarios"):  # a short list must not pass as a short run
            run_node("", [{"steps": []}, {"steps": []}], driver="console.log('[]')")

    def test_a_throw_the_snippet_swallows_is_an_error_not_a_kill(self):
        # The stub has no document.querySelector: the mutant throws inside the poll's .then, which the snippet's own
        # catch would hide as wrong counts.
        crash = self.snippet.replace("document.querySelectorAll(sel)", "document.querySelector(sel)")
        self.assertNotEqual(crash, self.snippet)
        with self.assertRaisesRegex(NodeFailed, "exited 1"):
            failures(crash)
        self.assertEqual(failures(self.snippet), [])  # its own 'offline' rejection is still ignored


class SkipReasonTest(unittest.TestCase):
    """Never skips: a missing node must say so loudly, so the node tests cannot pass silently."""

    def test_a_missing_node_skips_with_a_reason_that_names_node_and_the_issue(self):
        reason = skip_reason(lambda _name: None)
        self.assertTrue(reason.strip(), "the skip reason is empty")
        self.assertIn("node", reason)
        self.assertIn("#337", reason)

    def test_a_present_node_does_not_skip(self):
        self.assertEqual(skip_reason(lambda name: "/usr/bin/" + name), "")


if __name__ == "__main__":
    unittest.main()
