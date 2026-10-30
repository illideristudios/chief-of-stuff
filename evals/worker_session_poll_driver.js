// Exercise the served script with a small DOM, fake clock, and successive server snapshots.
const vm = require('vm');
const assert = require('assert');
const script = require('fs').readFileSync(0, 'utf8');
let offset = 0;
class Node {
  constructor(name, value = '', attrs = {}) {
    this.nodeName = name; this.nodeType = name === '#text' ? 3 : 1;
    this.value = value; this.attrs = attrs; this.childNodes = []; this.parent = null;
  }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({name, value})); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  get dataset() { return {ended: this.attrs['data-ended'], pending: this.attrs['data-pending']}; }
  appendChild(n) { n.parent = this; this.childNodes.push(n); }
  cloneNode(deep) { const n = new Node(this.nodeName, this.value, {...this.attrs}); if (deep) this.childNodes.forEach(c => n.appendChild(c.cloneNode(true))); return n; }
  isEqualNode(n) { return !!n && this.nodeName === n.nodeName && this.value === n.value &&
    JSON.stringify(this.attrs) === JSON.stringify(n.attrs) && this.childNodes.length === n.childNodes.length &&
    this.childNodes.every((c, i) => c.isEqualNode(n.childNodes[i])); }
  contains(n) { return this === n || this.childNodes.some(c => c.contains(n)); }
  replaceWith(n) { const p = this.parent, i = p.childNodes.indexOf(this); n.parent = p; p.childNodes[i] = n; this.parent = null;
    if (p.attrs.id === 'session-notice') offset += 25; }
  remove() { this.parent.childNodes.splice(this.parent.childNodes.indexOf(this), 1); this.parent = null; }
  querySelector(selector) { const id = selector.slice(1); return this.walk().find(n => n.attrs.id === id) || null; }
  walk() { return [this, ...this.childNodes.flatMap(c => c.walk())]; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === live; }
  getBoundingClientRect() { return {top: 100 + offset, bottom: 120 + offset}; }
  set textContent(v) { this.value = v; }
  get textContent() { return this.value; }
}
function element(name, id, children = [], attrs = {}) { const n = new Node(name, '', {...(id ? {id} : {}), ...attrs}); children.forEach(c => n.appendChild(c)); return n; }
function text(value) { return new Node('#text', value); }
function snapshot({ended = false, pending = true, event = 'one', extra = false, link = 'page-2', notice = 'old', message = 'queued'} = {}) {
  const eventNode = element('DIV', null, [text(event)], {class: 'session-event'});
  return element('DIV', 'session-live', [
    element('DIV', 'session-notice', [text(notice)]),
    element('DIV', 'session-events', [eventNode, ...(extra ? [element('DIV', null, [text('two')], {class:'session-event'})] : [])]),
    element('DIV', 'session-pagination', [element('P', null, [element('A', null, [text('Next')], {href: link})])]),
    element('DIV', 'session-messages', [text(message)])
  ], {'data-ended': String(ended), 'data-pending': String(pending)});
}
let live = snapshot(), fresh = snapshot(), timer, cleared = false, scroll = [];
const state = {textContent: 'Recorded as running'}, freshState = {textContent: 'Recorded as running'};
const textarea = {value: 'unsent draft', readOnly: false}, button = {disabled: false}, meta = {textContent: ''};
const form = {addEventListener() {}, querySelector(q) { return {textarea, button, '.meta': meta}[q]; }};
let freshForm = true;
const document = {activeElement: live.querySelector('#session-pagination').childNodes[0].childNodes[0],
  querySelector(q) { return q === '.compose' ? form : q === '#session-live' ? live : q === '#session-state' ? state : null; },
  querySelectorAll(q) { return live.querySelector('#session-events').childNodes; }};
const otherDoc = {querySelector(q) { return q === '#session-live' ? fresh : q === '#session-state' ? freshState : q === '.compose' && freshForm ? form : null; }};
const sandbox = {document, window: {scrollBy: (x, y) => scroll.push([x,y])}, location: {href:'/workers/sessions/1?cursor=old'},
  DOMParser: class {parseFromString() { return otherDoc; }}, fetch: async (url, opts) => { assert.equal(url, sandbox.location.href); assert.equal(opts.cache, 'no-store'); return {ok:true, text:async()=>''}; },
  setInterval: fn => {timer = fn; return 9;}, clearInterval: id => {assert.equal(id, 9); cleared = true;}, TextEncoder};
vm.runInNewContext(script.replace(/^<script>|<\/script>$/g, ''), sandbox);
(async () => {
  const focused = document.activeElement, first = live.querySelector('#session-events').childNodes[0];
  await timer();
  assert.strictEqual(document.activeElement, focused); assert.strictEqual(live.querySelector('#session-pagination').childNodes[0].childNodes[0], focused);
  assert.strictEqual(live.querySelector('#session-events').childNodes[0], first);
  fresh = snapshot({extra:true, message:'sending', link:'page-3', notice:'new'});
  await timer();
  assert.strictEqual(live.querySelector('#session-events').childNodes[0], first); assert.equal(live.querySelector('#session-events').childNodes.length, 2);
  assert.strictEqual(live.querySelector('#session-pagination').childNodes[0].childNodes[0], focused); assert.equal(focused.attrs.href, 'page-2');
  assert.equal(live.querySelector('#session-messages').childNodes[0].value, 'sending'); assert.deepEqual(scroll.at(-1), [0,25]);
  fresh = snapshot({ended:true, pending:true, extra:true, message:'sending', link:'page-3', notice:'new'}); freshState.textContent = 'Ended'; freshForm = false;
  await timer(); assert.equal(state.textContent, 'Ended'); assert.equal(cleared, false); assert.equal(button.disabled, true);
  assert.equal(textarea.readOnly, true); assert.equal(textarea.value, 'unsent draft');
  fresh = snapshot({ended:true, pending:false, extra:true, message:'delivered', link:'page-3', notice:'new'});
  await timer(); assert.equal(cleared, false); assert.equal(live.querySelector('#session-messages').childNodes[0].value, 'delivered');
  document.activeElement = null; await timer(); assert.equal(cleared, true);
  assert.equal(live.querySelector('#session-pagination').childNodes[0].childNodes[0].attrs.href, 'page-3');
})().catch(e => {console.error(e); process.exitCode = 1;});
