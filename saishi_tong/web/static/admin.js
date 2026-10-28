'use strict';
(() => {
  const byId = (id) => document.getElementById(id);
  const login = byId('admin-login');
  if (!login) return;
  let csrf = '', authenticated = false, currentId = null, generation = 0, busy = false;
  let currentFingerprint = '', originalDeadline = '', initialDeadlineDate = '';
  const deadlineDate = (raw) => {
    const m = /^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?$/.exec(raw || '');
    if (!m) return '';
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    const value = m[1] + '-' + m[2].padStart(2, '0') + '-' + m[3].padStart(2, '0');
    return Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === value ? value : '';
  };
  let verification = 'unverified', page = 1, pages = 1, query = '', pending = null;
  const workspace = byId('admin-workspace'), editor = byId('admin-editor');
  const list = byId('admin-contest-list'), sourceList = byId('admin-sources');
  const editorPanel = byId('admin-editor-panel');
  let pollTimer = null;
  let mergeReviewKey = null, lastActive = false, pollFailures = 0, jobRead = 0;
  let selectedIds = new Set(), editorDirty = false, editorTrigger = null, searchTimer = null, lastJobs = null;
  const fields = ['contest_name', 'raw_title', 'summary', 'organizer', 'deadline', 'status', 'format', 'location_scope', 'timeline', 'eligibility', 'prizes', 'signup_url', 'publication_state'];
  const note = (text) => { byId('admin-feedback').textContent = text; byId('admin-editor-feedback').textContent = text; };
  const toast = (text, kind) => {
    note(text);
    const oc = byId('openclaw-feedback');
    if (oc) { oc.textContent = text; oc.dataset.kind = kind || 'ok'; }
  };
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const clearPending = () => {
    pending = null; mergeReviewKey = null;
    byId('openclaw-merge-review').hidden = true;
    byId('openclaw-merge-review').replaceChildren();
  };
  const checked = (contest) => contest.human_reviewed === true;
  const reviewLabel = (contest) => contest.verification_label || (contest.human_reviewed ? (contest.luna_state === 'verified' ? 'AI核验＋人工校对' : '人工校对') : contest.luna_state === 'verified' ? 'AI核验完成·待人工确认' : '未核验');
  const visibility = (contest) => {
    if (contest.publication_state === 'retired' || contest.merged_into) return '游客不可见：已退役或合并。';
    if (contest.publication_state !== 'published') return '游客不可见：当前为草稿。';
    if (contest.freshness_state && contest.freshness_state !== 'current') return '游客不可见：' + contest.freshness_reason;
    if (!checked(contest) && contest.luna_state === 'verified') return '游客不可见：AI核验完成，待人工确认。';
    if (!checked(contest)) return contest.luna_state === 'stale' ? '游客不可见：内容已变化，需重新核验或完成人工校对。' : '游客不可见：已设为发布，需完成人工校对或确认。';
    return '游客可见：' + reviewLabel(contest) + '，已发布。';
  };
  const resetEditor = () => {
    const wasOpen = editorPanel.open;
    if (wasOpen) editorPanel.close();
    editor.inert = false; editorPanel.removeAttribute('aria-busy');
    ['admin-save', 'admin-editor-close', 'admin-discard'].forEach(id => { byId(id).disabled = false; });
    editorDirty = false; byId('admin-discard').hidden = true; byId('admin-delete-review').hidden = true;
    currentId = null; currentFingerprint = ''; originalDeadline = initialDeadlineDate = ''; byId('admin-deadline-original').textContent = '日期未知时可留空。'; editor.reset(); sourceList.replaceChildren();
    byId('contest-human-review').value = 'no'; byId('admin-verification-state').textContent = '当前核验：未核验';
    editorPanel.hidden = true; byId('admin-source-details').open = false;
    byId('admin-editor-title').textContent = '新建赛事';
    byId('admin-publish').disabled = true; byId('admin-delete').disabled = true;
    byId('admin-curate-current').disabled = true;
    byId('admin-visibility').textContent = '新建赛事默认为草稿；完成核验并发布后，游客才可浏览。';
    byId('admin-editor-visibility').textContent = '新建草稿 · 保存并核验后才能公开';
    if (wasOpen) editorTrigger?.focus?.();
  };
  const showEditor = () => {
    editorPanel.hidden = false;
    if (!editorPanel.open) editorPanel.showModal();
    byId('admin-editor-feedback').textContent = '';
  };
  const requestClose = () => {
    if (editorPanel.open && busy) { note('正在处理赛事，请稍候再关闭。'); return false; }
    if (editorDirty) {
      note('有未保存的改动，请先保存，或选择“放弃更改并关闭”。');
      byId('admin-discard').hidden = false; return false;
    }
    resetEditor(); return true;
  };
  const syncSelection = () => {
    const checks = list.querySelectorAll('.admin-select');
    checks.forEach(input => { input.checked = selectedIds.has(input.value); });
    byId('admin-selected-count').textContent = '已选 ' + selectedIds.size + ' 条';
    byId('admin-curate-selected').disabled = !selectedIds.size;
    byId('admin-clear-selection').disabled = !selectedIds.size;
    const all = byId('admin-select-page'); all.disabled = !checks.length;
    all.checked = !!checks.length && selectedIds.size === checks.length;
    all.indeterminate = selectedIds.size > 0 && !all.checked;
  };
  const clearSelection = () => { selectedIds.clear(); syncSelection(); };
  const setView = (name) => {
    const jobs = name === 'jobs';
    byId('admin-records-view').hidden = name !== 'contests';
    byId('admin-sources-view').hidden = name !== 'sources'; byId('admin-jobs-view').hidden = !jobs; byId('admin-trash-view').hidden = name !== 'trash';
    for (const view of ['contests', 'sources', 'jobs', 'trash']) byId('admin-view-' + view).setAttribute('aria-selected', String(name === view));
    if (!jobs && mergeReviewKey) { clearPending(); refreshJobs(); }
    window.history?.replaceState(null, '', '#' + name);
  };
  const clearUI = () => {
    generation += 1; csrf = ''; authenticated = false; busy = false; workspace.removeAttribute('aria-busy');
    window.dispatchEvent(new CustomEvent('saishi:admin-auth', { detail: { authenticated: false } }));
    byId('admin-token').value = ''; workspace.hidden = true;
    byId('admin-login-panel').hidden = false;
    list.replaceChildren(); byId('admin-trash-list').replaceChildren(); byId('admin-total').textContent = '';
    clearSelection(); lastJobs = null; setView('contests');
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    verification = 'unverified'; page = 1; pages = 1; query = ''; byId('admin-query').value = '';
    byId('admin-tab-awaiting').textContent = '人工待核（0）';
    byId('admin-tab-verified').textContent = '已核验（0）'; byId('admin-tab-unverified').textContent = '未核验（0）';
    byId('admin-page').textContent = '第 1 / 1 页';
    byId('admin-prev').disabled = true; byId('admin-next').disabled = true;
    byId('admin-page-top').textContent = '第 1 / 1 页';
    byId('admin-prev-top').disabled = true; byId('admin-next-top').disabled = true;
    resetEditor(); clearPending();
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    lastActive = false; pollFailures = 0; jobRead += 1;
    byId('openclaw-jobs')?.replaceChildren(); if (byId('openclaw-feedback')) byId('openclaw-feedback').textContent = '';
    byId('source-intake-url').value = ''; byId('source-intake-feedback').textContent = '';
  };
  const request = async (path, method = 'GET', body, publicRequest = false) => {
    if (!publicRequest && !authenticated) throw new Error('请先登录管理员会话。');
    const started = generation;
    const response = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: { ...(method === 'GET' || !csrf ? {} : { 'X-CSRF-Token': csrf }), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('服务器未返回有效数据，请稍后重试。'); }
    if (started !== generation) { const error = new Error('会话已切换。'); error.stale = true; throw error; }
    if (!response.ok || data.ok !== true) {
      const detail = data.detail && typeof data.detail === 'object' ? data.detail : {};
      const reason = typeof data.error === 'string' ? data.error : (typeof detail.error === 'string' ? detail.error : (typeof data.detail === 'string' ? data.detail : '操作未完成（HTTP ' + response.status + '）。'));
      const error = new Error(reason);
      error.status = response.status; error.setupHint = typeof data.setup_hint === 'string' ? data.setup_hint : (typeof detail.setup_hint === 'string' ? detail.setup_hint : '');
      if (response.status === 401 && !publicRequest) {
        const session = await request('/api/admin/session', 'GET', undefined, true);
        if (session.authenticated !== true) clearUI();
      }
      throw error;
    }
    return data;
  };
  const reportError = (error) => {
    if (error.stale) return;
    if (error.status === 409) {
      clearPending(); note('记录或合并建议已变化，请重新打开候选并核对。'); refreshJobs();
    } else if (error.status === 401) {
      note('管理员会话已失效，请重新输入令牌。');
    } else if (error.status === 403) {
      note('权限或 CSRF 校验未通过，请刷新页面恢复会话后重试。');
    } else note(error.message || '操作失败，请检查网络后重试。');
  };
  const run = async (action) => {
    if (busy) return;
    const started = generation; busy = true; workspace.setAttribute('aria-busy', 'true');
    try { await action(); } catch (error) { reportError(error); }
    finally { if (started === generation) { busy = false; workspace.removeAttribute('aria-busy'); } }
  };
  const commitEditor = (action, progress = '正在保存赛事…') => run(async () => {
    const started = generation;
    const buttons = ['admin-save', 'admin-publish', 'admin-delete', 'admin-delete-confirm', 'admin-delete-cancel', 'admin-confirm-human', 'admin-curate-current', 'admin-editor-close', 'admin-discard'];
    editor.inert = true; editorPanel.setAttribute('aria-busy', 'true');
    buttons.forEach(id => { byId(id).disabled = true; });
    note(progress);
    try { await action(); }
    finally {
      if (started === generation) {
        editor.inert = false; editorPanel.removeAttribute('aria-busy');
        buttons.forEach(id => { byId(id).disabled = ['admin-publish', 'admin-delete', 'admin-curate-current'].includes(id) && currentId === null; });
      }
    }
  });
  function addSource(source = {}) {
    const row = element('div', undefined, 'source-row');
    for (const [name, title] of [['url', '来源 URL'], ['title', '来源标题'], ['excerpt', '原文片段']]) {
      const label = element('label', title);
      const input = element(name === 'excerpt' ? 'textarea' : 'input'); input.dataset.sourceField = name;
      if (name === 'url') { input.type = 'url'; input.placeholder = 'https://'; }
      if (name === 'excerpt') input.rows = 3;
      input.value = typeof source[name] === 'string' ? source[name] : '';
      label.append(input); row.append(label);
    }
    const remove = element('button', '移除此来源', 'btn'); remove.type = 'button';
    remove.addEventListener('click', () => { row.remove(); editorDirty = true; }); row.append(remove); sourceList.append(row);
  }
  const populate = (contest) => {
    currentId = contest.id; currentFingerprint = contest.record_fingerprint || '';
    byId('contest-human-review').value = contest.human_reviewed ? 'yes' : 'no';
    byId('admin-verification-state').textContent = '当前核验：' + reviewLabel(contest) + (contest.human_reviewed_at ? ' · 人工校对时间：' + localTime(contest.human_reviewed_at) : '');
    for (const field of fields) {
      const aliases = { summary: 'snippet', organizer: 'host', prizes: 'prize' };
      const value = contest[field] || (field === 'raw_title' ? contest.title : contest[aliases[field]]) || '';
      editor.elements.namedItem(field).value = typeof value === 'string' ? value : '';
    }
    originalDeadline = contest.deadline || ''; initialDeadlineDate = deadlineDate(originalDeadline);
    editor.elements.namedItem('deadline').value = initialDeadlineDate;
    byId('admin-deadline-original').textContent = originalDeadline ? '原始时间线索：' + originalDeadline + '。日期未改时保留原文；选择新日期将替换为日期。' : '日期未知时可留空；选择日期后按北京时间日历日展示。';
    if (!editor.elements.namedItem('status').value) editor.elements.namedItem('status').value = '未知';
    if (!editor.elements.namedItem('location_scope').value) editor.elements.namedItem('location_scope').value = '未知';
    if (!editor.elements.namedItem('format').value) editor.elements.namedItem('format').value = '未知';
    if (!editor.elements.namedItem('publication_state').value) editor.elements.namedItem('publication_state').value = 'draft';
    sourceList.replaceChildren();
    const sources = Array.isArray(contest.sources) ? contest.sources : [];
    sources.forEach((source) => addSource(source));
    if (!sources.length) addSource(contest.source_url ? { url: contest.source_url, title: contest.raw_title || contest.title || '' } : {});
    byId('admin-editor-title').textContent = '编辑赛事';
    byId('admin-visibility').textContent = visibility(contest);
    byId('admin-editor-visibility').textContent = visibility(contest);
    byId('admin-publish').disabled = false; byId('admin-source-details').open = false;
    byId('admin-curate-current').disabled = false; byId('admin-delete').disabled = false;
    editorDirty = false; byId('admin-discard').hidden = true; byId('admin-delete-review').hidden = true; showEditor();
  };
  const sourceLink = (parent, contest) => {
    const urls = [contest.source_url, ...(Array.isArray(contest.sources) ? contest.sources.map((source) => source.url) : [])];
    const url = urls.find((value) => typeof value === 'string' && /^https?:\/\//i.test(value));
    if (url) {
      const link = element('a', url, 'source-url helper'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; parent.append(link);
    } else parent.append(element('p', '来源 URL 待补充核验', 'helper'));
  };
  const refresh = async () => {
    const data = await request('/api/admin/contests?verification=' + verification + '&page=' + page + '&page_size=15&q=' + encodeURIComponent(query));
    page = Number(data.page) || 1; pages = Math.max(1, Number(data.pages) || 1);
    if (page > pages) { page = pages; return refresh(); }
    list.replaceChildren();
    const currentIds = new Set((data.contests || []).map(c => c.id));
    selectedIds = new Set([...selectedIds].filter(id => currentIds.has(id)));
    const counts = data.counts || {};
    byId('admin-tab-unverified').textContent = '未核验（' + String(counts.unverified ?? 0) + '）';
    byId('admin-tab-awaiting').textContent = '人工待核（' + String(counts.awaiting ?? 0) + '）';
    byId('admin-tab-verified').textContent = '已核验（' + String(counts.verified ?? 0) + '）';
    for (const name of ['unverified', 'awaiting', 'verified']) byId('admin-tab-' + name).setAttribute('aria-selected', String(name === verification));
    byId('admin-list-panel').setAttribute('aria-labelledby', 'admin-tab-' + verification);
    byId('admin-list-title').textContent = verification === 'verified' ? '已核验赛事' : verification === 'awaiting' ? '人工待核赛事' : '未核验赛事';
    byId('admin-total').textContent = '共 ' + String(data.total ?? 0) + ' 条 · 每页 15 条';
    byId('admin-page').textContent = '第 ' + page + ' / ' + pages + ' 页';
    byId('admin-prev').disabled = page <= 1; byId('admin-next').disabled = page >= pages;
    byId('admin-page-top').textContent = byId('admin-page').textContent;
    byId('admin-prev-top').disabled = page <= 1; byId('admin-next-top').disabled = page >= pages;
    for (const contest of (data.contests || []).slice(0, 15)) {
      const item = element('article', undefined, 'admin-list-item');
      const edit = element('button', contest.contest_name || '待核验赛事', 'admin-item-title'); edit.type = 'button';
      edit.setAttribute('aria-haspopup', 'dialog'); edit.setAttribute('aria-controls', 'admin-editor-panel');
      edit.title = '打开赛事编辑抽屉';
      edit.addEventListener('click', () => run(async () => {
        if (!requestClose()) return;
        editorTrigger = edit;
        const detail = await request('/api/admin/contests/' + encodeURIComponent(contest.id)); populate(detail.contest);
        note('在抽屉中编辑赛事，保存后生效。');
      }));
      item.append(edit);
      const selectLabel = element('label', undefined, 'admin-select-label');
      const select = element('input'); select.type = 'checkbox'; select.className = 'admin-select'; select.value = contest.id;
      select.setAttribute('aria-label', '选择赛事：' + (contest.contest_name || contest.id));
      select.addEventListener('change', () => { if (select.checked) selectedIds.add(contest.id); else selectedIds.delete(contest.id); syncSelection(); });
      selectLabel.append(select); item.append(selectLabel);
      const lunaState = contest.luna_state || contest.luna?.state || 'pending';
      const stateLabels = { verified: 'AI核验完成·待人工确认', pending: '待核验', stale: '待重新核验', disabled: '未启用核验', failed: '核验失败', review: 'OpenClaw 已整理 · 部分待核验' };
      const shownStatus = contest.current_status || contest.status || '未知';
      item.append(element('p', (contest.publication_state === 'published' ? '已发布' : '草稿') + ' · ' + shownStatus + (shownStatus !== contest.status ? '（按日期更新）' : '') + ' · ' + (contest.format || '未知') + ' · ' + (checked(contest) ? reviewLabel(contest) : (stateLabels[lunaState] || '未核验')), 'helper'));
      item.append(element('p', visibility(contest), 'helper admin-visibility'));
      item.append(element('p', '检索时间：' + (contest.search_time || '未知'), 'helper'));
      sourceLink(item, contest);
      const curate = element('button', checked(contest) ? 'OpenClaw 重新整理' : 'OpenClaw 整理/核验', 'btn admin-verify'); curate.type = 'button';
      curate.addEventListener('click', () => run(async () => {
        curate.disabled = true; curate.textContent = '提交中…';
        try {
          const result = await request('/api/admin/openclaw/jobs', 'POST', { scope: 'selected', contest_ids: [contest.id] });
          setView('jobs'); toast('已加入整理队列：「' + (contest.contest_name || contest.id) + '」。', 'ok');
          await refreshJobs(); return result;
        } finally {
          curate.disabled = false;
          curate.textContent = checked(contest) ? 'OpenClaw 重新整理' : 'OpenClaw 整理/核验';
        }
      }));
      item.append(curate);
      list.append(item);
    }
    if (!data.contests?.length) {
      const empty = element('div', undefined, 'admin-empty');
      empty.append(element('p', query ? '没有匹配的赛事，请调整关键词。' : (verification === 'verified' ? '暂无已核验赛事。' : verification === 'awaiting' ? '暂无人工待核赛事。' : '暂无未核验赛事。'), 'helper'));
      if (!query && verification === 'unverified') empty.append(element('p', '可点上方「让 OpenClaw 整理未核验」批量核验，或点行内「OpenClaw 整理/核验」。', 'helper'));
      if (!query && verification === 'awaiting') empty.append(element('p', 'AI核验完成的赛事会在此等待人工确认；打开赛事核对来源后点“人工确认并保存”。', 'helper'));
      if (!query && verification === 'verified') empty.append(element('p', '人工确认后会出现在这里；已发布的赛事才会进入公开目录。', 'helper'));
      list.append(empty);
    }
    syncSelection();
  };
  const jobLabels = { queued: '排队中', running: '整理中', done: '已完成', failed: '失败', cancelled: '已取消' };
  const itemLabels = { pending: '等待', running: '整理中', done: '完成', failed: '失败', skipped: '跳过' };
  const verdictLabels = { verified: 'AI核验完成·待人工确认', needs_review: '待核验', not_contest: '疑似非赛事' };
  const localTime = (iso) => {
    const value = new Date(typeof iso === 'number' && iso < 1e12 ? iso * 1000 : iso);
    return iso && Number.isFinite(value.getTime()) ? value.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) + '（北京时间）' : '未记录';
  };
  const openMergeReview = async (job, item) => {
    clearPending();
    const key = job.id + '/' + item.contest_id;
    const path = '/api/admin/openclaw/jobs/' + encodeURIComponent(job.id) + '/items/' + encodeURIComponent(item.contest_id);
    mergeReviewKey = key;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const panel = byId('openclaw-merge-review');
    try {
      const data = await request(path + '/merge-preview');
      if (mergeReviewKey !== key) return;
      panel.hidden = false;
      panel.append(element('h3', '核对是否为同一届赛事'));
      panel.append(element('p', '勾选确定重复的条目，再查看合并结果预览。核对期间暂停任务自动刷新。', 'helper'));
      const choices = [], labels = { verified: 'AI核验完成·待人工确认', pending: '待核验', stale: '待重新核验', review: '部分待核验', failed: '核验失败' };
      let prepared = null;
      const result = element('div', undefined, 'merge-result');
      const preflight = element('button', '预览所选合并', 'btn'); preflight.type = 'button'; preflight.disabled = true;
      const confirm = element('button', '确认执行合并', 'btn-primary'); confirm.type = 'button'; confirm.hidden = true;
      for (const record of data.contests || []) {
        const box = element('article', undefined, 'merge-record');
        if (record.id === item.contest_id) box.append(element('h4', '当前条目：' + record.contest_name));
        else {
          const label = element('label', undefined, 'merge-choice');
          const input = element('input'); input.type = 'checkbox'; input.value = record.id;
          input.addEventListener('change', () => { prepared = null; result.replaceChildren(); confirm.hidden = true; preflight.disabled = !choices.some(c => c.checked); });
          choices.push(input); label.append(input); label.append(element('span', record.contest_name || record.id)); box.append(label);
        }
        box.append(element('p', (labels[record.luna_state] || '未核验') + ' · ' + (record.publication_state === 'published' ? '已设为发布' : '草稿'), 'helper'));
        box.append(element('p', '主办：' + (record.organizer || '待核验') + '；截止：' + (record.deadline || '待核验'), 'helper'));
        if (record.timeline) box.append(element('p', record.timeline, 'helper'));
        sourceLink(box, record);
        box.append(element('p', '检索时间：' + (record.search_time || '未知'), 'helper')); panel.append(box);
      }
      preflight.addEventListener('click', () => run(async () => {
        const selected = choices.filter(c => c.checked).map(c => c.value);
        if (!selected.length) return;
        const snapshot = await request(path + '/merge-preview', 'POST', { duplicate_ids: selected });
        if (mergeReviewKey !== key) return;
        if (selected.join(',') !== choices.filter(c => c.checked).map(c => c.value).join(',')) return;
        prepared = snapshot; result.replaceChildren();
        result.append(element('h4', '将保留「' + snapshot.survivor_name + '」，其余 ' + snapshot.retired_ids.length + ' 条退役'));
        result.append(element('p', '来源和审计会保留。主记录非空事实不被覆盖；补入未核验事实可能需要重新核验。', 'helper'));
        for (const diff of snapshot.comparisons || []) {
          const field = element('details', undefined, 'merge-difference');
          field.append(element('summary', diff.label + (diff.conflict ? ' · 存在差异' : ' · 有待补充')));
          for (const value of diff.values) {
            const name = snapshot.contests.find(c => c.id === value.id)?.contest_name || value.id;
            field.append(element('p', name + '：' + (value.value || '未提供'), 'helper'));
          }
          result.append(field);
        }
        confirm.hidden = false;
      }));
      confirm.addEventListener('click', () => run(async () => {
        if (!prepared || mergeReviewKey !== key) return;
        const merged = await request(path + '/merge', 'POST', { duplicate_ids: prepared.duplicate_ids, expected_fingerprints: prepared.fingerprints });
        clearPending(); toast('合并成功：保留「' + merged.survivor_name + '」，下线 ' + merged.retired_ids.length + ' 条。', 'ok');
        await refresh(); await refreshJobs();
      }));
      const keep = element('button', '保留独立，不合并', 'btn'); keep.type = 'button';
      keep.addEventListener('click', () => run(async () => {
        await request(path + '/keep-separate', 'POST', {});
        clearPending(); toast('已保留独立记录，本次合并建议已处理。', 'ok'); await refreshJobs();
      }));
      const close = element('button', '暂不处理', 'btn'); close.type = 'button';
      close.addEventListener('click', () => run(async () => { clearPending(); await refreshJobs(); }));
      const actions = element('div', undefined, 'actions');
      actions.append(preflight); actions.append(confirm); actions.append(keep); actions.append(close);
      panel.append(result); panel.append(actions); panel.focus(); panel.scrollIntoView?.({ block: 'nearest' });
    } catch (error) {
      if (mergeReviewKey === key) { clearPending(); await refreshJobs(); }
      throw error;
    }
  };
  const renderJobs = (data) => {
    const box = byId('openclaw-jobs'); if (!box) return false;
    lastJobs = data;
    box.replaceChildren();
    const jobs = data.jobs || [];
    byId('admin-view-jobs').textContent = '整理任务（' + jobs.length + '）';
    const mode = byId('openclaw-job-filter').value || 'all';
    const needsAttention = job => (job.items || []).some(item => item.status === 'failed' || (item.merge_ids?.length && !item.merged)) || job.status === 'failed';
    const visible = jobs.filter(job => mode === 'active' ? ['queued', 'running'].includes(job.status) : mode === 'attention' ? needsAttention(job) : true);
    const badge = byId('openclaw-worker');
    const seen = data.worker && data.worker.seen_at ? new Date(data.worker.seen_at) : null;
    const online = seen && (Date.now() - seen.getTime()) < 10 * 60 * 1000;
    badge.textContent = online ? 'Jarvis 在线' : (seen ? 'Jarvis 最近在线：' + localTime(data.worker.seen_at) : 'Jarvis 未连接');
    const active = jobs.some(job => ['queued', 'running'].includes(job.status));
    for (const job of visible) {
      const card = element('article', undefined, 'openclaw-job');
      if (job.kind === 'source') {
        card.append(element('h3', '来源解析 · ' + job.source_name + ' · ' + (jobLabels[job.status] || job.status)));
        sourceLink(card, { source_url: job.source_url });
        card.append(element('p', '提交：' + localTime(job.created_at) + (job.model ? ' · 实际模型：' + job.model : ''), 'helper'));
        const results = job.results || [];
        card.append(element('p', job.status === 'done' ? '新增 ' + results.length + ' 条待核验草稿 · 跳过 ' + (job.skipped || 0) + ' 条已有或无效线索。' : (job.error || '由 Jarvis 领取后解析，新线索不会自动公开。'), 'helper'));
        for (const result of results) {
          const resultBox = element('article', undefined, 'source-result');
          const open = element('button', '核对草稿：' + result.contest_name, 'btn source-draft-open'); open.type = 'button';
          open.addEventListener('click', () => run(async () => {
            const data = await request('/api/admin/contests/' + encodeURIComponent(result.id));
            editorTrigger = open; populate(data.contest); note('这是来源解析草稿，请核对来源、逐条核验后再发布。');
          })); resultBox.append(open); sourceLink(resultBox, result);
          resultBox.append(element('p', '检索时间：' + (result.search_time || '未记录，请打开草稿核对'), 'helper'));
          card.append(resultBox);
        }
        if (['queued', 'running'].includes(job.status)) {
          const cancel = element('button', '取消来源解析', 'btn'); cancel.type = 'button';
          cancel.addEventListener('click', () => run(async () => { await request('/api/admin/openclaw/source-jobs/' + encodeURIComponent(job.id) + '/cancel', 'POST', {}); await refreshJobs(); })); card.append(cancel);
        } else if (job.status === 'failed') {
          const retry = element('button', '重新解析此来源', 'btn source-retry'); retry.type = 'button';
          retry.addEventListener('click', () => run(async () => {
            await request('/api/admin/openclaw/source-jobs', 'POST', { source_id: job.source_id, url: job.source_url, limit: job.item_limit });
            toast('已重新提交来源解析。'); await refreshJobs();
          })); card.append(retry);
        }
        box.append(card); continue;
      }
      const c = job.counts || {};
      const head = (jobLabels[job.status] || job.status) + ' · ' + (job.scope === 'selected' ? '指定赛事' : '未核验批量') + ' · ' + String(job.total || 0) + ' 条 · 完成 ' + String(c.done || 0) + ' / 失败 ' + String(c.failed || 0);
      card.append(element('h3', head));
      const progress = element('progress'); progress.max = Math.max(1, job.total || 0);
      progress.value = (c.done || 0) + (c.failed || 0) + (c.skipped || 0);
      progress.setAttribute('aria-label', '任务处理进度'); card.append(progress);
      const v = job.verdicts || {};
      card.append(element('p', '提交：' + localTime(job.created_at) + (job.model ? ' · 模型：' + job.model : '') + ' · AI通过 ' + String(v.verified || 0) + '，待核验 ' + String(v.needs_review || 0) + (v.not_contest ? '，疑似非赛事 ' + String(v.not_contest) : ''), 'helper'));
      if (job.error) card.append(element('p', '错误：' + job.error, 'helper'));
      if (job.heartbeat_at) card.append(element('p', '最近心跳：' + localTime(job.heartbeat_at), 'helper'));
      const ul = element('ul', undefined, 'openclaw-items');
      for (const it of job.items || []) {
        const li = element('li');
        li.append(element('strong', it.contest_name || it.contest_id));
        li.append(element('span', ' · ' + (itemLabels[it.status] || it.status) + (it.verdict ? ' · ' + (verdictLabels[it.verdict] || it.verdict) : '')));
        if (it.summary) li.append(element('p', it.summary, 'helper'));
        if (Array.isArray(it.merge_ids) && it.merge_ids.length && !it.merged && it.status === 'done') {
          const preview = element('div', undefined, 'openclaw-merge-preview');
          preview.append(element('p', '发现 ' + it.merge_ids.length + ' 条可能重复。请核对赛事名称、届次、主办和时间。', 'helper'));
          const mb = element('button', '核对重复候选', 'btn openclaw-merge-btn'); mb.type = 'button';
          mb.addEventListener('click', () => run(() => openMergeReview(job, it)));
          preview.append(mb);
          li.append(preview);
        } else if (it.merged) {
          li.append(element('p', '✓ 已合并', 'helper openclaw-merged-flag'));
        }
        ul.append(li);
      }
      const results = element('details', undefined, 'openclaw-result-details');
      results.open = ['queued', 'running'].includes(job.status) || needsAttention(job);
      results.append(element('summary', '查看 ' + (job.items || []).length + ' 条处理结果')); results.append(ul); card.append(results);
      const failedIds = (job.items || []).filter(it => it.status === 'failed').map(it => it.contest_id);
      if (failedIds.length && job.status !== 'queued' && job.status !== 'running') {
        const retry = element('button', '仅重试失败的 ' + failedIds.length + ' 条', 'btn openclaw-retry'); retry.type = 'button';
        retry.addEventListener('click', () => run(async () => {
          await request('/api/admin/openclaw/jobs', 'POST', { scope: 'selected', contest_ids: failedIds });
          toast('失败条目已重新入队，已完成的条目不会重跑。', 'ok'); await refreshJobs();
        }));
        card.append(retry);
      }
      if (job.status === 'queued' || job.status === 'running') {
        const cancel = element('button', '取消任务', 'btn'); cancel.type = 'button';
        cancel.addEventListener('click', () => run(async () => { await request('/api/admin/openclaw/jobs/' + encodeURIComponent(job.id) + '/cancel', 'POST', {}); await refreshJobs(); }));
        card.append(cancel);
      }
      box.append(card);
    }
    if (!visible.length) box.append(element('p', jobs.length ? '当前筛选下没有任务。' : '还没有整理任务。', 'helper'));
    return active;
  };
  async function refreshJobs() {
    if (!authenticated || mergeReviewKey) return;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const serial = ++jobRead;
    try {
      const data = await request('/api/admin/openclaw/jobs?limit=10');
      if (serial !== jobRead || mergeReviewKey) return;
      const active = renderJobs(data);
      if (pollFailures) byId('openclaw-feedback').textContent = '任务状态连接已恢复。';
      pollFailures = 0;
      if (lastActive && !active) { try { await refresh(); } catch (_) { /* next list refresh can recover */ } }
      lastActive = active;
      if (active && authenticated) pollTimer = setTimeout(refreshJobs, document.hidden ? 30000 : 8000);
    } catch (error) {
      if (error.stale || !authenticated || serial !== jobRead || mergeReviewKey) return;
      pollFailures += 1;
      const delay = Math.min(60000, 8000 * 2 ** Math.min(pollFailures - 1, 3));
      byId('openclaw-feedback').textContent = '暂时无法读取任务状态，已保留上次结果，将在 ' + delay / 1000 + ' 秒后重试。';
      pollTimer = setTimeout(refreshJobs, delay);
    }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && authenticated && !mergeReviewKey) refreshJobs(); });
  byId('openclaw-job-filter').addEventListener('change', () => { if (lastJobs) { clearPending(); renderJobs(lastJobs); refreshJobs(); } });
  const restore = async () => {
    const session = await request('/api/admin/session', 'GET', undefined, true);
    if (session.authenticated !== true) { clearUI(); note('请输入管理员令牌以加载工作台，访客不能查看草稿。'); return; }
    if (typeof session.csrf_token !== 'string' || !session.csrf_token) throw new Error('会话缺少 CSRF 凭据，请重新登录。');
    csrf = session.csrf_token; authenticated = true;
    byId('admin-token').value = ''; byId('admin-login-panel').hidden = true; workspace.hidden = false;
    window.dispatchEvent(new CustomEvent('saishi:admin-auth', { detail: { authenticated: true } }));
    await refresh(); refreshJobs(); note('已登录管理员，刷新页面保持会话。');
    byId('admin-feedback').title = session.expires_at ? '会话到期：' + localTime(session.expires_at) : '';
  };
  login.addEventListener('submit', (event) => {
    event.preventDefault(); if (busy) return;
    const input = byId('admin-token');
    if (!input.value.trim()) { note('请输入管理员令牌。'); return; }
    run(async () => {
      try {
        // No retained JS token: the input is consumed only for this login request.
        const result = request('/api/admin/login', 'POST', { token: input.value.trim() }, true);
        input.value = ''; await result; await restore();
      } finally { input.value = ''; }
    });
  });
  byId('admin-logout').addEventListener('click', () => run(async () => {
    try { await request('/api/admin/logout', 'POST', {}); clearUI(); note('已退出管理员会话，列表、编辑和核验确认均已清空。'); }
    catch (error) { clearUI(); note('页面数据已清空，但无法确认服务端退出；请重新连接后再次退出。' + error.message); }
  }));
  byId('admin-refresh').addEventListener('click', () => run(async () => { if (!requestClose()) return; clearPending(); await refresh(); await refreshJobs(); note('工作台已刷新。'); }));
  byId('admin-new').addEventListener('click', () => { if (!busy && authenticated && requestClose()) { editorTrigger = byId('admin-new'); addSource(); showEditor(); note('正在新建草稿，保存后生效。'); } });
  byId('admin-add-source').addEventListener('click', () => { addSource(); editorDirty = true; });
  byId('admin-editor-close').addEventListener('click', requestClose);
  byId('admin-discard').addEventListener('click', () => { resetEditor(); note('已放弃未保存的修改。'); });
  editorPanel.addEventListener('cancel', (event) => { event.preventDefault(); requestClose(); });
  editor.addEventListener('input', () => { editorDirty = true; });
  editor.addEventListener('change', () => { editorDirty = true; byId('admin-editor-visibility').textContent = '有未保存改动；可见性以保存结果为准。'; });
  window.addEventListener('beforeunload', (event) => { if (editorDirty && authenticated) { event.preventDefault(); event.returnValue = ''; } });
  for (const view of ['contests', 'sources', 'jobs', 'trash']) {
    const tab = byId('admin-view-' + view);
    tab.addEventListener('click', () => { setView(view); if (view === 'jobs') refreshJobs(); if (view === 'trash') refreshTrash(); });
    tab.addEventListener('keydown', (event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        const views = ['contests', 'sources', 'jobs', 'trash'];
        event.preventDefault(); const target = event.key === 'Home' ? 'contests' : event.key === 'End' ? 'jobs' : views[(views.indexOf(view) + (event.key === 'ArrowLeft' ? 3 : 1)) % 4];
        byId('admin-view-' + target).focus(); byId('admin-view-' + target).click();
      }
    });
  }
  byId('admin-select-page').addEventListener('change', (event) => {
    selectedIds.clear(); if (event.target.checked) list.querySelectorAll('.admin-select').forEach(input => selectedIds.add(input.value)); syncSelection();
  });
  byId('admin-clear-selection').addEventListener('click', clearSelection);
  byId('admin-curate-selected').addEventListener('click', () => run(async () => {
    if (!selectedIds.size) return;
    const data = await request('/api/admin/openclaw/jobs', 'POST', { scope: 'selected', contest_ids: [...selectedIds] });
    clearSelection(); setView('jobs'); toast('已将所选 ' + String(data.job?.total || 0) + ' 条加入整理队列。', 'ok'); await refreshJobs();
  }));
  for (const name of ['unverified', 'awaiting', 'verified']) {
    const switchTab = () => run(async () => { if (!requestClose()) return; verification = name; page = 1; clearSelection(); clearPending(); await refresh(); });
    byId('admin-tab-' + name).addEventListener('click', switchTab);
    byId('admin-tab-' + name).addEventListener('keydown', (event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); const tabs = ['unverified', 'awaiting', 'verified']; const target = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[2] : tabs[(tabs.indexOf(name) + (event.key === 'ArrowLeft' ? 2 : 1)) % 3];
        byId('admin-tab-' + target).focus?.(); byId('admin-tab-' + target).click();
      }
    });
  }
  const search = () => {
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    if (!authenticated) return;
    if (busy) { searchTimer = setTimeout(search, 150); return; }
    run(async () => { if (!requestClose()) return; query = byId('admin-query').value.trim(); page = 1; clearSelection(); clearPending(); await refresh(); });
  };
  byId('admin-search').addEventListener('submit', (event) => { event.preventDefault(); search(); });
  byId('admin-query').addEventListener('change', search);
  byId('admin-query').addEventListener('input', () => { if (searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(search, 300); });
  byId('admin-clear-search').addEventListener('click', () => { byId('admin-query').value = ''; search(); });
  byId('admin-prev').addEventListener('click', () => { if (page > 1) run(async () => { if (!requestClose()) return; page -= 1; clearSelection(); clearPending(); await refresh(); }); });
  byId('admin-next').addEventListener('click', () => { if (page < pages) run(async () => { if (!requestClose()) return; page += 1; clearSelection(); clearPending(); await refresh(); }); });
  byId('admin-prev-top').addEventListener('click', () => byId('admin-prev').click());
  byId('admin-next-top').addEventListener('click', () => byId('admin-next').click());
  byId('openclaw-refresh')?.addEventListener('click', () => run(async () => { clearPending(); await refreshJobs(); }));
  byId('openclaw-batch')?.addEventListener('click', () => run(async () => {
    const btn = byId('openclaw-batch');
    const limit = Number(byId('openclaw-limit').value) || 1;
    if (btn) { btn.disabled = true; btn.textContent = '提交中…'; }
    try {
      const data = await request('/api/admin/openclaw/jobs', 'POST', { scope: 'unverified', limit });
      setView('jobs');
      toast('已提交 ' + String(data.job?.total || 0) + ' 条进入整理队列；Jarvis 约 2 分钟内拉取。', 'ok');
      await refreshJobs();
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '整理未核验 ↗'; }
    }
  }));
  byId('source-intake-id').addEventListener('change', () => {
    const required = byId('source-intake-id').value === 'url';
    byId('source-intake-url').required = required;
    byId('source-intake-url-label').textContent = required ? '页面链接（必填）' : '页面链接（可选）';
    byId('source-intake-url').placeholder = required ? '粘贴具体赛事或活动的公开链接' : '留空读取所选平台；也可粘贴具体赛事链接';
  });
  byId('source-intake-form').addEventListener('submit', (event) => {
    event.preventDefault(); if (!authenticated || busy) return;
    const sourceId = byId('source-intake-id').value, url = byId('source-intake-url').value.trim();
    if (sourceId === 'url' && !url) { note('请填写具体赛事或活动链接。'); return; }
    run(async () => {
      const button = byId('source-intake-submit'); button.disabled = true;
      try {
        await request('/api/admin/openclaw/source-jobs', 'POST', { source_id: sourceId, url, limit: Number(byId('source-intake-limit').value) || 3 });
        byId('source-intake-feedback').textContent = '已提交手动解析，新线索将作为私有草稿收集。';
        setView('jobs'); toast('来源解析已入队，可在整理任务中查看；不会自动发布。'); await refreshJobs();
      } finally { button.disabled = false; }
    });
  });
  editor.addEventListener('submit', (event) => {
    event.preventDefault(); if (!authenticated || busy) return;
    const body = {};
    for (const field of fields) if (field !== 'raw_title') body[field] = editor.elements.namedItem(field).value.trim();
    if (currentId !== null && currentFingerprint) body.expected_fingerprint = currentFingerprint;
    if (body.deadline === initialDeadlineDate) body.deadline = originalDeadline;
    body.human_reviewed = byId('contest-human-review').value === 'yes';
    body.sources = Array.from(sourceList.querySelectorAll('.source-row')).map((row) => {
      const source = {}; row.querySelectorAll('[data-source-field]').forEach((input) => { source[input.dataset.sourceField] = input.value.trim(); }); return source;
    }).filter((source) => source.url || source.title || source.excerpt);
    if (body.sources.some((source) => !/^https?:\/\//i.test(source.url || ''))) { note('每条来源必须包含 http(s) URL。'); return; }
    if (body.signup_url && !/^https?:\/\//i.test(body.signup_url)) { note('报名链接必须为 http(s) URL。'); return; }
    commitEditor(async () => {
      const data = await request(currentId === null ? '/api/admin/contests' : '/api/admin/contests/' + encodeURIComponent(currentId), currentId === null ? 'POST' : 'PUT', body);
      clearPending(); populate(data.contest); await refresh(); note('赛事已保存。' + visibility(data.contest));
    });
  });
  async function refreshTrash() {
    try {
      const data = await request('/api/admin/trash'); const box = byId('admin-trash-list'); box.replaceChildren();
      for (const contest of data.contests || []) {
        const row = element('article', undefined, 'online-result'); row.append(element('h3', contest.contest_name || '待核对赛事'));
        row.append(element('p', '删除时间：' + localTime(contest.deleted_at) + ' · 检索时间：' + (contest.search_time || '未记录'), 'helper')); sourceLink(row, contest);
        const restore = element('button', '恢复赛事', 'btn'); restore.type = 'button';
        restore.addEventListener('click', () => run(async () => {
          await request('/api/admin/contests/' + encodeURIComponent(contest.id) + '/restore', 'POST', { expected_fingerprint: contest.record_fingerprint });
          await refreshTrash(); await refresh(); note('赛事已恢复，保留删除前的核验和发布状态。');
        })); row.append(restore); box.append(row);
      }
      if (!data.contests?.length) box.append(element('p', '回收站为空。', 'helper'));
    } catch (error) { reportError(error); }
  }
  byId('admin-refresh-trash').addEventListener('click', refreshTrash);
  byId('admin-delete').addEventListener('click', () => {
    if (currentId === null || busy) return;
    if (editorDirty) { note('请先保存或放弃当前修改，再删除赛事。'); return; }
    byId('admin-delete-review').hidden = false; byId('admin-delete-cancel').focus();
  });
  byId('admin-delete-cancel').addEventListener('click', () => { byId('admin-delete-review').hidden = true; byId('admin-delete').focus(); });
  byId('admin-delete-confirm').addEventListener('click', () => {
    if (currentId === null || busy || editorDirty || byId('admin-delete-review').hidden) return;
    commitEditor(async () => {
      await request('/api/admin/contests/' + encodeURIComponent(currentId), 'DELETE', { expected_fingerprint: currentFingerprint });
      resetEditor(); await refresh(); note('赛事已移入回收站，可在“回收站”恢复。');
    }, '正在移入回收站…');
  });
  byId('admin-confirm-human').addEventListener('click', () => {
    if (!authenticated || busy) return;
    byId('contest-human-review').value = 'yes'; editorDirty = true; editor.requestSubmit();
  });
  byId('admin-publish').addEventListener('click', () => {
    if (currentId === null) return;
    if (editorDirty) { note('请先保存当前修改，再发布。'); return; }
    commitEditor(async () => { const data = await request('/api/admin/contests/' + encodeURIComponent(currentId) + '/publish', 'POST', {}); clearPending(); populate(data.contest); await refresh(); note('已保存发布状态。' + visibility(data.contest)); });
  });
  byId('admin-curate-current').addEventListener('click', () => {
    if (currentId === null) return;
    if (editorDirty) { note('请先保存当前修改，再提交核验。'); return; }
    commitEditor(async () => {
      await request('/api/admin/openclaw/jobs', 'POST', { scope: 'selected', contest_ids: [currentId] });
      resetEditor(); setView('jobs'); toast('当前赛事已加入核验队列，核验完成后仍需确认发布状态。'); await refreshJobs();
    }, '正在提交核验…');
  });
  // Navigation/refresh clears page memory only, never the HttpOnly server cookie.
  window.addEventListener('pagehide', clearUI);
  window.addEventListener('pageshow', (event) => { if (event.persisted) run(restore); });
  resetEditor(); setView(['#jobs', '#sources', '#trash'].includes(window.location?.hash) ? window.location.hash.slice(1) : 'contests'); run(restore);
})();
