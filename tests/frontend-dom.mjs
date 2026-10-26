import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
const app = await readFile(new URL('../saishi_tong/web/static/app.js', import.meta.url), 'utf8');
const admin = await readFile(new URL('../saishi_tong/web/static/admin.js', import.meta.url), 'utf8');
class Node {
  constructor(tag = 'div') { this.tagName = tag; this.listeners = {}; this.children = []; this.dataset = {}; this.value = ''; this.textContent = ''; this.hidden = false; this.attributes = {}; this.classList = { add() {}, remove() {} }; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  fire(type, extra = {}) { const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra }; for (const listener of this.listeners[type] || []) listener(event); return event; }
  append(child) { child.parent = this; this.children.push(child); }
  replaceChildren() { this.children = []; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { return this.children.flatMap(child => [(selector === '.source-row' && child.className === 'source-row') || (selector === '[data-source-field]' && child.dataset.sourceField) || (selector === '.button-label' && child.className === 'button-label') ? child : null, ...child.querySelectorAll(selector)]).filter(Boolean); }
  reset() { for (const field of Object.values(this.fields || {})) field.value = ''; if (this.fields) { this.fields.status.value = '未知'; this.fields.format.value = '未知'; this.fields.publication_state.value = 'draft'; } }
}
function setup(ids) {
  const nodes = Object.fromEntries(ids.map(id => [id, new Node()]));
  const data = new Map();
  const window = new Node(); window.dispatchEvent = e => window.fire(e.type);
  const context = { document: { getElementById: id => nodes[id] || null, createElement: tag => new Node(tag) }, window, Event: class { constructor(type) { this.type = type; } }, localStorage: { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)), removeItem: key => data.delete(key) }, navigator: {}, console };
  return { nodes, data, window, context };
}
const ID = 'TENCENTCLOUD_SECRET_ID', KEY = 'TENCENTCLOUD_SECRET_KEY', USE = 'saishi_use_local_credentials';
{
  const s = setup(['settings-form', 'settings-feedback', 'secret-id', 'secret-key', 'refresh-credentials', 'clear-credentials']);
  vm.runInNewContext(app, s.context);
  s.nodes['secret-id'].value = 'synthetic-id'; s.nodes['secret-key'].value = 'synthetic-key';
  s.nodes['settings-form'].fire('submit');
  assert.equal(s.data.get(ID), 'synthetic-id'); assert.equal(s.data.get(KEY), 'synthetic-key'); assert.equal(s.data.has(USE), false);
  s.nodes['secret-key'].value = ''; s.nodes['refresh-credentials'].fire('click'); assert.equal(s.nodes['secret-key'].value, 'synthetic-key');
  s.nodes['clear-credentials'].fire('click'); assert.equal(s.data.size, 0); assert.equal(s.nodes['secret-key'].value, '');
}
{
  const s = setup(['search-form', 'go', 'search-feedback']);
  const label = new Node(); label.className = 'button-label'; s.nodes.go.append(label);
  vm.runInNewContext(app, s.context);
  s.context.localStorage.getItem = () => { throw new Error('search must not read storage'); };
  s.context.fetch = () => { throw new Error('search must not fetch'); };
  s.data.set(ID, 'synthetic-id'); s.data.set(KEY, 'synthetic-key'); s.data.set(USE, 'true');
  const payload = new Map([['q', 'AI']]);
  s.nodes['search-form'].fire('formdata', { formData: payload });
  assert.equal(payload.size, 1); assert.equal(s.nodes['search-form'].listeners.formdata, undefined);
  assert.equal(s.nodes['search-form'].fire('submit').defaultPrevented, false);
  assert.equal(s.nodes['search-form'].children.length, 0, 'no hidden credential DOM');

}
// Admin cookie/session/CRUD/verify regressions live in the dedicated fixture.
await import('./frontend-admin-session.mjs');
await import('./frontend-online.mjs');
assert(!admin.includes('innerHTML')); assert(!app.includes('innerHTML'));
{
  const s = setup(['directory-filters']); let submits = 0;
  s.nodes['directory-filters'].requestSubmit = () => { submits += 1; };
  s.context.localStorage.getItem = () => { throw Error('filters must not read credentials'); };
  s.context.fetch = () => { throw Error('filters use normal GET navigation'); };
  vm.runInNewContext(app, s.context);
  const groups = { status: ['', '报名中', '即将截止'], format: ['', '线上', '线下', '混合'], location: ['', '国内', '国外', '未知'] };
  for (const [name, values] of Object.entries(groups)) groups[name] = values.map(value => ({ name, value, checked: !value, matches: selector => selector === 'input[type="checkbox"]' }));
  s.nodes['directory-filters'].querySelectorAll = selector => groups[selector.match(/name="([^"]+)"/)[1]];
  const change = (name, i, checked) => { const target = groups[name][i]; target.checked = checked; s.nodes['directory-filters'].fire('change', { target }); };
  change('status', 1, true); assert.equal(groups.status[0].checked, false);
  change('status', 2, true); assert(groups.status[1].checked && groups.status[2].checked);
  change('format', 1, true); assert(groups.status[1].checked && groups.status[2].checked, 'other group preserved');
  change('status', 0, true); assert(groups.status[0].checked && !groups.status[1].checked && !groups.status[2].checked);
  change('format', 1, false); assert(groups.format[0].checked, 'last removal selects all');
  assert.equal(submits, 5);
  s.nodes['directory-filters'].fire('change', { target: { matches: () => false } });
  assert.equal(submits, 5, 'typing remains a normal form submission');
  change('format', 2, true); change('location', 1, true); change('location', 2, true);
  assert(groups.location[1].checked && groups.location[2].checked && !groups.location[0].checked);
  change('format', 2, false); assert(groups.location[0].checked && !groups.location[1].checked && !groups.location[2].checked);
}
{
  const s = setup(['settings-form', 'settings-feedback', 'secret-id', 'secret-key', 'refresh-credentials', 'clear-credentials']);
  s.context.fetch = () => { throw Error('admin authentication must never download CAM'); };
  vm.runInNewContext(app, s.context);
  s.window.fire('saishi:admin-auth', { detail: { authenticated: true } });
  s.window.fire('saishi:admin-auth', { detail: { authenticated: false } });
  assert.equal(s.data.size, 0, 'authentication does not save credentials');
}
const template = async (name) => readFile(new URL('../saishi_tong/web/templates/' + name, import.meta.url), 'utf8');
assert.match(await template('index.html'), /method="get" action="\/contests"/);
assert(!((await template('index.html')).includes('type="hidden"')));
assert.match(await template('contests.html'), /name="status" value=""/);
assert.match(await template('_contest.html'), /c\.contest_name\|default\('待核验赛事'/);
assert.match(await template('detail.html'), /contest\.raw_title/);
assert.match(await template('base.html'), /id="settings-dropdown"/);
assert.match(await template('base.html'), /href="\/admin"/);
assert.match(await template('base.html'), /href="\/settings"/);

console.log('PASS frontend DOM: local keys; local search no storage/network/keys; multi-select filters/all exclusion; no CAM autofill; admin session/CRUD/publish/OpenClaw/XSS; template contracts');
