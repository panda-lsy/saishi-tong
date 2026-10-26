import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
const script = await readFile(new URL('../saishi_tong/web/static/admin.js', import.meta.url), 'utf8');
const template = await readFile(new URL('../saishi_tong/web/templates/_admin.html', import.meta.url), 'utf8');
class Node {
  constructor(tag = 'div') { this.tagName = tag; this.listeners = {}; this.children = []; this.dataset = {}; this.value = ''; this.hidden = false; this.attributes = {}; this.disabled = false; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  fire(type, extra = {}) { const event = { preventDefault() {}, ...extra }; for (const listener of this.listeners[type] || []) listener(event); }
  click() { if (!this.disabled) this.fire('click'); }
  requestSubmit() { this.fire('submit'); }
  focus() { this.focused = true; } scrollIntoView() { this.scrolled = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  append(child) { child.parent = this; this.children.push(child); }
  replaceChildren() { this.children = []; this._text = ''; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelectorAll(selector) {
    const matches = child => selector.startsWith('.') ? (child.className || '').split(' ').includes(selector.slice(1)) : selector === '[data-source-field]' ? !!child.dataset.sourceField : selector === 'input' ? child.tagName === 'input' : false;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  reset() { for (const field of Object.values(this.fields || {})) field.value = ''; if (this.fields) { this.fields.status.value = this.fields.format.value = '未知'; this.fields.publication_state.value = 'draft'; } }
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (body, status = 200) => ({ status, ok: status < 400, json: async () => body });
const makeContest = i => ({ id: String(i), contest_name: i === 1 ? '<img src=x onerror=alert(1)>' : '赛事 ' + i, raw_title: '原始报道', luna_state: 'pending', status: '未知', format: '线上', publication_state: 'published', source_url: 'https://example.test/' + i, search_time: '2026-10-22T00:00:00Z', sources: [{ url: 'https://example.test/' + i, title: '<script>literal</script>', excerpt: '<img onerror=bad>' }] });
function fixture(signedIn = true) {
  const nodes = Object.fromEntries([...template.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Node()]));
  for (const id of ['admin-workspace', 'admin-editor-panel']) nodes[id].hidden = true;
  nodes['openclaw-limit'].value = '1';
  const fields = ['contest_name', 'raw_title', 'summary', 'organizer', 'deadline', 'status', 'format', 'location_scope', 'timeline', 'eligibility', 'prizes', 'signup_url', 'publication_state'];
  const editor = nodes['admin-editor']; editor.fields = Object.fromEntries(fields.map(field => [field, new Node()])); editor.elements = { namedItem: name => editor.fields[name] };
  const requests = [], window = new Node(); window.dispatchEvent = event => window.fire(event.type, event); window.confirm = () => true;
  let contests = Array.from({ length: 66 }, (_, i) => makeContest(i + 1));
  let deleted = [];
  let jobs = [], createStatus = 200, deferred = null, jobsUnavailable = false, saveStatus = 200, saveDeferred = null; const timers = new Map(); let timer = 0;
  const ctx = { document: { hidden: false, addEventListener() {}, getElementById: id => nodes[id] || null, createElement: tag => new Node(tag) }, window, console,
    CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    setTimeout: (callback, delay) => { timers.set(++timer, { callback, delay }); return timer; }, clearTimeout: id => timers.delete(id),
    localStorage: { getItem() { throw Error('no admin storage'); }, setItem() { throw Error('no admin storage'); } },
    fetch: async (path, options) => {
      requests.push({ path, ...options });
      assert.equal(options.credentials, 'same-origin'); assert.equal(options.headers.Authorization, undefined);
      if (path === '/api/admin/session') return response({ ok: true, authenticated: signedIn, ...(signedIn ? { csrf_token: 'synthetic-csrf', expires_at: 1792713600 } : {}) });
      const body = options.body ? JSON.parse(options.body) : {};
      if (path === '/api/admin/login') { signedIn = body.token === 'synthetic-admin'; return response({ ok: signedIn }, signedIn ? 200 : 403); }
      if (options.method !== 'GET') assert.equal(options.headers['X-CSRF-Token'], 'synthetic-csrf');
      if (path === '/api/admin/logout') { signedIn = false; return response({ ok: true }); }
      if (!signedIn) return response({ ok: false }, 401);
      if (path.startsWith('/api/admin/openclaw/jobs?')) { if (jobsUnavailable) return response({ ok: false, detail: 'offline' }, 503); return response({ ok: true, jobs, worker: null }); }
      if (path === '/api/admin/openclaw/source-jobs') {
        if (createStatus !== 200) return response({ ok: false, detail: '来源暂不可用' }, createStatus);
        jobs = [{ id: 'source-test', kind: 'source', status: 'queued', source_id: body.source_id, source_name: '魔搭', source_url: body.url || 'https://modelscope.cn/events', item_limit: body.limit, created_at: '2026-10-23T00:00:00Z', results: [] }];
        return response({ ok: true, job: jobs[0] });
      }
      if (path === '/api/admin/openclaw/jobs') {
        if (deferred) return deferred;
        if (createStatus !== 200) return response({ ok: false, detail: 'worker 暂不可用' }, createStatus);
        const id = body.contest_ids?.[0] || '1';
        const job = { id: 'job-test', status: 'queued', scope: body.scope, total: 1, counts: {}, verdicts: {}, created_at: '2026-10-22T00:00:00Z', items: [{ contest_id: id, contest_name: '<img onerror=bad>', status: 'pending' }] };
        jobs = [job]; return response({ ok: true, job });
      }
      if (path.endsWith('/cancel')) { jobs[0].status = 'cancelled'; return response({ ok: true }); }
      if (path.endsWith('/merge-preview')) return response({ ok: true, survivor_id: '1', survivor_name: '保留赛事', duplicate_ids: ['2'], retired_ids: ['2'], fingerprints: { '1': 'a'.repeat(64), '2': 'b'.repeat(64) }, contests: [{ ...makeContest(1), luna_state: 'verified' }, makeContest(2)], comparisons: [{ label: '主办', field: 'organizer', conflict: true, values: [{ id: '1', value: '<img onerror=bad>' }, { id: '2', value: '其他主办' }] }] });
      if (path.endsWith('/keep-separate')) { jobs[0].items[0].merge_ids = []; return response({ ok: true }); }
      if (path.endsWith('/merge')) { jobs[0].items[0].merged = true; contests = contests.filter(c => c.id !== '2'); return response({ ok: true, survivor_name: '保留赛事', retired_ids: ['2'] }); }
      if (path.startsWith('/api/admin/contests?')) {
        const params = new URL(path, 'http://localhost').searchParams;
        assert.equal(params.get('page_size'), '15');
        const verified = contests.filter(item => item.human_reviewed), awaiting = contests.filter(item => !item.human_reviewed && (item.luna_state === 'verified' || item.ai_reviewed_at)), unverified = contests.filter(item => !item.human_reviewed && item.luna_state !== 'verified' && !item.ai_reviewed_at);
        let filtered = params.get('verification') === 'verified' ? verified : params.get('verification') === 'awaiting' ? awaiting : unverified;
        const q = params.get('q'); if (q) filtered = filtered.filter(item => item.contest_name.includes(q));
        const page = Math.max(1, Number(params.get('page'))); const pages = Math.max(1, Math.ceil(filtered.length / 15));
        return response({ ok: true, contests: filtered.slice((page - 1) * 15, page * 15), total: filtered.length, page, page_size: 15, pages, counts: { verified: verified.length, awaiting: awaiting.length, unverified: unverified.length } });
      }
      if (path === '/api/admin/trash') return response({ ok: true, contests: deleted });
      if (options.method === 'DELETE') { const id = path.split('/')[4]; const card = contests.find(c => c.id === id); deleted.push({ ...card, deleted_at: '2026-10-24T00:00:00Z' }); contests = contests.filter(c => c.id !== id); return response({ ok: true, contest: deleted.at(-1) }); }
      if (path.endsWith('/restore')) { const id = path.split('/')[4]; const card = deleted.find(c => c.id === id); deleted = deleted.filter(c => c.id !== id); contests.push({ ...card, deleted_at: '' }); return response({ ok: true }); }
      if (path.endsWith('/publish')) { const contest = contests.find(item => item.id === path.split('/')[4]); contest.publication_state = 'published'; return response({ ok: true, contest }); }
      if (options.method === 'POST' || options.method === 'PUT') {
        if (saveDeferred) return saveDeferred;
        if (saveStatus !== 200) return response({ ok: false, detail: '保存暂时失败' }, saveStatus);
        const id = options.method === 'POST' ? '67' : path.split('/')[4];
        const previous = contests.find(item => item.id === id);
        const contest = { ...makeContest(Number(id)), publication_state: 'draft', ...previous, ...body, id };
        contests = contests.filter(item => item.id !== id); contests.unshift(contest); return response({ ok: true, contest });
      }
      return response({ ok: true, contest: contests.find(item => item.id === path.split('/')[4]) });
    }
  };
  vm.runInNewContext(script, ctx);
  return { nodes, window, requests, timers,
    finishSourceJob() { jobs[0].status = 'done'; jobs[0].model = 'openai/gpt-6-luna'; jobs[0].results = [{ id: '67', contest_name: '<img onerror=bad>来源草稿' }]; contests.push({ ...makeContest(67), publication_state: 'draft', luna_state: 'review' }); },
    setSaveStatus: value => { saveStatus = value; }, deferSave: value => { saveDeferred = value; },
    setJobsUnavailable: value => { jobsUnavailable = value; }, setCreateStatus: value => { createStatus = value; }, defer: value => { deferred = value; },
    setContest: (id, changes) => Object.assign(contests.find(c => c.id === id), changes),
    failJob() { jobs[0].status = 'done'; jobs[0].items[0].status = 'failed'; jobs[0].items.push({ contest_id: '2', status: 'done' }); },
    finishJob() { jobs[0].status = 'done'; jobs[0].counts = { done: 1 }; jobs[0].items[0] = { ...jobs[0].items[0], status: 'done', merge_ids: ['2'], verdict: 'verified' }; },
  };
}
const click = async (s, id) => { s.nodes[id].click(); await settle(); };
const curate = async s => { s.nodes['admin-contest-list'].querySelectorAll('.admin-verify')[0].click(); await settle(); };
{
  const s = fixture(); await settle();
  assert.equal(s.nodes['admin-workspace'].hidden, false); assert.equal(s.nodes['admin-login-panel'].hidden, true);
  assert(!s.nodes['admin-feedback'].textContent.includes('1970'), 'Unix-second expiry is displayed as a real local date');
  assert.equal(s.nodes['admin-contest-list'].children.length, 15); assert.equal(s.nodes['admin-editor-panel'].hidden, true);
  assert.equal(s.nodes['admin-tab-unverified'].textContent, '未核验（66）');
  assert.equal(s.nodes['admin-contest-list'].children[0].children[0].textContent, '<img src=x onerror=alert(1)>');
  assert.match(s.nodes['admin-contest-list'].textContent, /游客不可见：已设为发布，需完成人工校对或确认/);
  await click(s, 'admin-next'); assert.equal(s.nodes['admin-page'].textContent, '第 2 / 5 页');
  await click(s, 'admin-prev'); assert.equal(s.nodes['admin-page'].textContent, '第 1 / 5 页');
  await click(s, 'admin-tab-verified'); assert.equal(s.nodes['admin-contest-list'].querySelectorAll('.admin-empty').length, 1);
  s.window.fire('pagehide'); assert.equal(s.nodes['admin-workspace'].hidden, true);
  assert(!s.requests.some(r => r.path.endsWith('/logout')), 'navigation preserves cookie session');
  s.window.fire('pageshow', { persisted: true }); await settle(); assert.equal(s.nodes['admin-workspace'].hidden, false);
}
{
  const s = fixture(false); await settle();
  s.nodes['admin-token'].value = 'wrong'; s.nodes['admin-login'].fire('submit'); await settle(); assert.equal(s.nodes['admin-workspace'].hidden, true);
  s.nodes['admin-token'].value = 'synthetic-admin'; s.nodes['admin-login'].fire('submit'); assert.equal(s.nodes['admin-token'].value, ''); await settle();
  assert.equal(s.nodes['admin-workspace'].hidden, false);
  await click(s, 'admin-new'); const editor = s.nodes['admin-editor']; editor.fields.contest_name.value = '测试草稿'; editor.fields.raw_title.value = '不提交';
  const source = s.nodes['admin-sources'].querySelectorAll('[data-source-field]'); source[0].value = 'https://example.test/source'; source[1].value = '<script>literal</script>';
  editor.fire('submit'); await settle(); const saved = s.requests.find(r => r.path === '/api/admin/contests' && r.method === 'POST');
  assert.equal(JSON.parse(saved.body).raw_title, undefined); assert.match(s.nodes['admin-feedback'].textContent, /当前为草稿/);
  editor.fields.summary.value = 'edit'; editor.fire('submit'); await settle(); assert(s.requests.some(r => r.method === 'PUT'));
  await click(s, 'admin-publish'); assert.match(s.nodes['admin-feedback'].textContent, /需完成人工校对或确认/);
  assert(!s.nodes['admin-feedback'].textContent.includes('现可浏览'));
  await click(s, 'admin-logout'); assert.equal(s.nodes['admin-workspace'].hidden, true); assert.equal(s.nodes['admin-contest-list'].children.length, 0);
  assert(!s.requests.filter(r => r.path !== '/api/admin/login').some(r => (r.body || '').includes('synthetic-admin')));
}
{
  const s = fixture(); await settle(); s.setContest('1', { luna_state: 'verified', human_reviewed: true, publication_state: 'draft' });
  await click(s, 'admin-tab-verified');
  assert.match(s.nodes['admin-contest-list'].textContent, /游客不可见：当前为草稿/);
  s.nodes['admin-contest-list'].children[0].children[0].click(); await settle();
  assert.equal(s.nodes['admin-editor-panel'].hidden, false); assert.match(s.nodes['admin-visibility'].textContent, /当前为草稿/);
  await click(s, 'admin-publish'); assert.match(s.nodes['admin-feedback'].textContent, /游客可见：AI核验＋人工校对，已发布/);
}
{
  const s = fixture(); await settle(); await click(s, 'openclaw-batch');
  assert.deepEqual(JSON.parse(s.requests.find(r => r.path === '/api/admin/openclaw/jobs').body), { scope: 'unverified', limit: 1 });
  assert(s.timers.size > 0, 'queued jobs keep polling');
  s.finishJob(); await click(s, 'openclaw-refresh');
  const buttons = s.nodes['openclaw-jobs'].querySelectorAll('.openclaw-merge-btn'); assert.equal(buttons.length, 1);
  buttons[0].click(); await settle();
  const panel = s.nodes['openclaw-merge-review']; assert.equal(panel.hidden, false);
  const checkbox = panel.querySelectorAll('input')[0]; checkbox.checked = true; checkbox.fire('change');
  const actions = panel.children.at(-1); actions.children[0].click(); await settle();
  assert.match(panel.textContent, /存在差异/); assert.match(panel.textContent, /<img onerror=bad>/);
  assert.equal(actions.children[1].hidden, false); actions.children[1].click(); await settle();
  assert.deepEqual(JSON.parse(s.requests.find(r => r.path.endsWith('/merge')).body), { duplicate_ids: ['2'], expected_fingerprints: { '1': 'a'.repeat(64), '2': 'b'.repeat(64) } });
  assert.match(s.nodes['admin-feedback'].textContent, /保留赛事/); assert.match(s.nodes['openclaw-jobs'].textContent, /已合并/);
}
{
  const s = fixture(); await settle(); s.setCreateStatus(503); await curate(s);
  assert.match(s.nodes['admin-feedback'].textContent, /worker 暂不可用/);
  assert.equal(s.nodes['admin-tab-unverified'].textContent, '未核验（66）');
  assert.equal(s.nodes['admin-contest-list'].querySelectorAll('.admin-verify')[0].disabled, false);
  s.setCreateStatus(200); await curate(s);
  const job = s.nodes['openclaw-jobs'].children[0]; job.children.at(-1).click(); await settle();
  assert(s.requests.some(r => r.path.endsWith('/cancel')));
}
{
  const s = fixture(); await settle(); let resolve; s.defer(new Promise(done => { resolve = done; }));
  s.nodes['admin-contest-list'].querySelectorAll('.admin-verify')[0].click(); s.window.fire('pagehide');
  resolve(response({ ok: true, job: { total: 1 } })); await settle();
  assert.equal(s.nodes['admin-workspace'].hidden, true); assert.equal(s.nodes['openclaw-jobs'].children.length, 0, 'late response cannot restore private UI');
}

{
  const s = fixture(); await settle(); await click(s, 'openclaw-batch');
  const previousJobs = s.nodes['openclaw-jobs'].textContent;
  const listReads = () => s.requests.filter(r => r.path.startsWith('/api/admin/contests?')).length;
  const before = listReads(); s.setJobsUnavailable(true); await click(s, 'openclaw-refresh');
  assert.equal(s.nodes['openclaw-jobs'].textContent, previousJobs, 'network errors preserve last task state');
  assert.equal(listReads(), before, 'network failure never means task completion');
  assert.match(s.nodes['openclaw-feedback'].textContent, /8 秒后重试/);
  const next = [...s.timers.values()].at(-1); assert.equal(next.delay, 8000);
  s.setJobsUnavailable(false); await next.callback(); await settle();
  assert.match(s.nodes['openclaw-feedback'].textContent, /连接已恢复/);
  s.finishJob(); await click(s, 'openclaw-refresh');
  s.nodes['openclaw-jobs'].querySelectorAll('.openclaw-merge-btn')[0].click(); await settle();
  const panel = s.nodes['openclaw-merge-review']; panel.children.at(-1).children[2].click(); await settle();
  assert(s.requests.some(r => r.path.endsWith('/keep-separate')));
  assert.equal(panel.hidden, true);
}
{
  const s = fixture(); await settle(); await click(s, 'openclaw-batch'); s.failJob(); await click(s, 'openclaw-refresh');
  const retry = s.nodes['openclaw-jobs'].querySelectorAll('.openclaw-retry')[0]; retry.click(); await settle();
  const submits = s.requests.filter(r => r.path === '/api/admin/openclaw/jobs');
  assert.deepEqual(JSON.parse(submits.at(-1).body), { scope: 'selected', contest_ids: ['1'] });
}
{
  const s = fixture(); await settle();
  await click(s, 'admin-next-top'); assert.equal(s.nodes['admin-page-top'].textContent, '第 2 / 5 页');
  await click(s, 'admin-prev-top');
  const checks = s.nodes['admin-contest-list'].querySelectorAll('.admin-select');
  checks[0].checked = true; checks[0].fire('change');
  checks[2].checked = true; checks[2].fire('change');
  assert.equal(s.nodes['admin-selected-count'].textContent, '已选 2 条');
  assert(s.nodes['admin-select-page'].indeterminate);
  await click(s, 'admin-curate-selected');
  assert.deepEqual(JSON.parse(s.requests.find(r => r.path === '/api/admin/openclaw/jobs').body), { scope: 'selected', contest_ids: ['1', '3'] });
  assert.equal(s.nodes['admin-records-view'].hidden, true); assert.equal(s.nodes['admin-jobs-view'].hidden, false);
  assert.equal(s.nodes['admin-selected-count'].textContent, '已选 0 条');
  await click(s, 'admin-view-contests');
  s.nodes['admin-select-page'].fire('change', { target: { checked: true } });
  assert.equal(s.nodes['admin-selected-count'].textContent, '已选 15 条');
  await click(s, 'admin-next-top'); assert.equal(s.nodes['admin-selected-count'].textContent, '已选 0 条');
  s.nodes['admin-query'].value = '赛事 20'; s.nodes['admin-query'].fire('input');
  const debounce = [...s.timers.values()].find(t => t.delay === 300); assert(debounce);
  debounce.callback(); await settle();
  assert.equal(s.nodes['admin-contest-list'].children.length, 1);
  await click(s, 'admin-clear-search'); assert.equal(s.nodes['admin-contest-list'].children.length, 15);
  s.nodes['openclaw-job-filter'].value = 'attention'; s.nodes['openclaw-job-filter'].fire('change'); await settle();
  assert.match(s.nodes['openclaw-jobs'].textContent, /当前筛选下没有任务/);
  assert(s.timers.size > 0, 'filtering out active jobs does not stop polling');
}
{
  const s = fixture(); await settle();
  const trigger = s.nodes['admin-contest-list'].children[0].children[0]; trigger.click(); await settle();
  const panel = s.nodes['admin-editor-panel'], editor = s.nodes['admin-editor'];
  assert(panel.open && !panel.hidden); assert(!panel.scrolled, 'drawer opens without scrolling the page');
  editor.fields.summary.value = '尚未保存'; editor.fire('input');
  await click(s, 'admin-editor-close'); assert(panel.open); assert.equal(s.nodes['admin-discard'].hidden, false);
  panel.fire('cancel'); assert(panel.open, 'Escape preserves unsaved work');
  let protectedUnload = false; s.window.fire('beforeunload', { preventDefault() { protectedUnload = true; } }); assert(protectedUnload);
  await click(s, 'admin-publish'); assert(!s.requests.some(r => r.path.endsWith('/publish')));
  s.setSaveStatus(503); editor.fire('submit'); await settle();
  assert(panel.open); assert.equal(editor.fields.summary.value, '尚未保存'); assert.match(s.nodes['admin-editor-feedback'].textContent, /保存暂时失败/);
  assert.equal(editor.inert, false); assert.equal(s.nodes['admin-save'].disabled, false);
  await click(s, 'admin-discard'); assert(!panel.open && panel.hidden); assert(trigger.focused);
  trigger.click(); await settle();
  let resolve; s.deferSave(new Promise(done => { resolve = done; }));
  editor.fields.summary.value = '提交快照'; editor.fire('input'); editor.fire('submit');
  assert(editor.inert); assert(s.nodes['admin-editor-close'].disabled); panel.fire('cancel'); assert(panel.open);
  resolve(response({ ok: false, detail: '连接中断' }, 503)); await settle();
  assert(!editor.inert); assert(panel.open); assert.equal(editor.fields.summary.value, '提交快照');
}
assert(!script.includes('innerHTML')); assert(!script.includes('localStorage')); assert(!script.includes('sessionStorage')); assert(!script.includes('document.cookie'));
{
  const s = fixture(); await settle(); await click(s, 'admin-view-sources');
  assert(!s.nodes['admin-sources-view'].hidden && s.nodes['admin-records-view'].hidden && s.nodes['admin-jobs-view'].hidden);
  s.nodes['source-intake-id'].value = 'url'; s.nodes['source-intake-id'].fire('change'); assert(s.nodes['source-intake-url'].required);
  assert.equal(s.nodes['source-intake-url-label'].textContent, '页面链接（必填）');
  s.nodes['source-intake-form'].fire('submit'); await settle(); assert(!s.requests.some(r => r.path.endsWith('/source-jobs')));
  s.nodes['source-intake-id'].value = 'modelscope'; s.nodes['source-intake-limit'].value = '3';
  s.setCreateStatus(503); s.nodes['source-intake-form'].fire('submit'); await settle();
  assert.match(s.nodes['admin-feedback'].textContent, /来源暂不可用/); assert(!s.nodes['source-intake-submit'].disabled);
  s.setCreateStatus(200); s.nodes['source-intake-form'].fire('submit'); await settle();
  assert.deepEqual(JSON.parse(s.requests.filter(r => r.path.endsWith('/source-jobs')).at(-1).body), { source_id: 'modelscope', url: '', limit: 3 });
  assert.equal(s.nodes['admin-jobs-view'].hidden, false); assert.match(s.nodes['openclaw-jobs'].textContent, /来源解析/);
  s.finishSourceJob(); await click(s, 'openclaw-refresh');
  assert.match(s.nodes['openclaw-jobs'].textContent, /新增 1 条待核验草稿/);
  assert.match(s.nodes['openclaw-jobs'].textContent, /<img onerror=bad>/);
  s.nodes['openclaw-jobs'].querySelectorAll('.source-draft-open')[0].click(); await settle();
  assert(s.nodes['admin-editor-panel'].open); assert.match(s.nodes['admin-visibility'].textContent, /当前为草稿/);
  await click(s, 'admin-curate-current');
  assert.equal(s.nodes['admin-editor-panel'].open, false);
  assert.deepEqual(JSON.parse(s.requests.filter(r => r.path === '/api/admin/openclaw/jobs').at(-1).body), { scope: 'selected', contest_ids: ['67'] });
  await click(s, 'admin-logout'); assert.equal(s.nodes['source-intake-url'].value, '');
}
assert(!script.includes('/auth/openai')); assert.match(template, /id="contest-raw-title"[^>]+readonly/);

{
  const s = fixture(); await settle();
  s.setContest('1', { luna_state: 'verified', human_reviewed: false });
  await click(s, 'admin-tab-awaiting');
  assert.equal(s.nodes['admin-tab-unverified'].textContent, '未核验（65）');
  assert.equal(s.nodes['admin-tab-awaiting'].textContent, '人工待核（1）');
  assert.match(s.nodes['admin-contest-list'].textContent, /AI核验完成·待人工确认/);
  s.nodes['admin-contest-list'].children[0].children[0].click(); await settle();
  assert.match(s.nodes['admin-visibility'].textContent, /AI核验完成，待人工确认/);
  await click(s, 'admin-confirm-human');
  assert.equal(JSON.parse(s.requests.find(r => r.method === 'PUT').body).human_reviewed, true);
  assert.equal(s.nodes['admin-tab-verified'].textContent, '已核验（1）');
  assert.match(s.nodes['admin-visibility'].textContent, /AI核验＋人工校对/);
}

{
  const s = fixture(); await settle();
  s.setContest('1', { deadline: '2026-11-22 15:59', human_reviewed: false });
  await click(s, 'admin-refresh'); s.nodes['admin-contest-list'].children[0].children[0].click(); await settle();
  assert.equal(s.nodes['admin-editor'].fields.deadline.value, '2026-11-22');
  s.nodes['admin-editor'].fire('submit'); await settle();
  assert.equal(JSON.parse(s.requests.find(r => r.method === 'PUT').body).deadline, '2026-11-22 15:59');
  s.nodes['admin-editor'].fields.deadline.value = '2026-12-01';
  s.nodes['admin-editor'].fire('submit'); await settle();
  assert.equal(JSON.parse(s.requests.filter(r => r.method === 'PUT').at(-1).body).deadline, '2026-12-01');
}
{
  const s = fixture(); await settle(); s.setContest('1', { deadline: '11月下旬，待公告' });
  await click(s, 'admin-refresh'); s.nodes['admin-contest-list'].children[0].children[0].click(); await settle();
  assert.equal(s.nodes['admin-editor'].fields.deadline.value, '');
  s.nodes['admin-editor'].fire('submit'); await settle();
  assert.equal(JSON.parse(s.requests.find(r => r.method === 'PUT').body).deadline, '11月下旬，待公告');
}

{
  const s = fixture(); await settle();
  s.nodes['admin-contest-list'].children[0].children[0].click(); await settle();
  await click(s, 'admin-delete'); assert(!s.nodes['admin-delete-review'].hidden);
  assert(!s.requests.some(r => r.method === 'DELETE'));
  await click(s, 'admin-delete-confirm'); assert(!s.nodes['admin-editor-panel'].open);
  assert.match(s.nodes['admin-feedback'].textContent, /已移入回收站/);
  assert.equal(s.nodes['admin-tab-unverified'].textContent, '未核验（65）');
  await click(s, 'admin-view-trash');
  const trash = s.nodes['admin-trash-list']; assert.equal(trash.children.length, 1);
  assert(trash.textContent.includes('检索时间')); assert(trash.textContent.includes('https://example.test/1'));
  trash.children[0].children.at(-1).click(); await settle(); await settle();
  assert.equal(s.nodes['admin-tab-unverified'].textContent, '未核验（66）');
}
console.log('PASS admin session DOM: restore/login/logout/CSRF/paging/CRUD/visibility/OpenClaw/merge/cancel/errors/XSS/late-response');
