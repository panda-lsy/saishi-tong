import { signRequest, directedQuery } from './online-signing.mjs';

const button = document.getElementById('online-find');
if (button) {
  const feedback = document.getElementById('online-feedback');
  const panel = document.getElementById('online-results');
  const items = document.getElementById('online-result-list');
  const optionsPanel = document.getElementById('online-options');
  const type = document.getElementById('online-type');
  const site = document.getElementById('online-site');
  const freshness = document.getElementById('online-freshness');
  const queryInput = document.getElementById('q');
  const preview = document.getElementById('online-query-preview');
  const query = () => directedQuery(queryInput.value, type?.value || '');
  const refreshPreview = () => { if (preview) preview.textContent = '实际检索词：' + query(); };
  const keys = () => ({ id: (localStorage.getItem('TENCENTCLOUD_SECRET_ID') || '').trim(), key: (localStorage.getItem('TENCENTCLOUD_SECRET_KEY') || '').trim() });
  const available = () => {
    try { const pair = keys(); button.hidden = !(pair.id && pair.key); }
    catch (_) { button.hidden = true; }
    if (optionsPanel) optionsPanel.hidden = button.hidden;
    refreshPreview();
  };
  const node = (tag, text, className) => {
    const el = document.createElement(tag); if (text !== undefined) el.textContent = text;
    if (className) el.className = className; return el;
  };
  let running = false, revision = 0;
  let controller = null;
  button.addEventListener('click', async () => {
    if (running) return;
    const actualQuery = query();
    panel.hidden = false;
    if (actualQuery.length > 300) { feedback.textContent = '实际检索词最多300字，请缩短关键词后重试。'; return; }
    const current = ++revision; controller = new AbortController();
    running = true; button.disabled = true; panel.hidden = false; items.replaceChildren();
    feedback.textContent = '正在使用本浏览器密钥在线查找；会消耗你的元宝搜索额度…';
    try {
      const pair = keys();
      if (!pair.id || !pair.key) throw new Error('missing_keys');
      if (actualQuery.includes(pair.id) || actualQuery.includes(pair.key)) throw new Error('credential_in_query');
      const envelope = await signRequest(actualQuery, pair.id, pair.key, undefined, { site: site?.value || '', freshness: freshness?.value || 'm3' });
      const sessionResponse = await fetch('/api/admin/session', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      const session = await sessionResponse.json();
      const response = await fetch('/api/online-search', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { 'Content-Type': 'application/json', ...(session.authenticated ? { 'X-CSRF-Token': session.csrf_token } : {}) },
        body: JSON.stringify(envelope)
      });
      const data = await response.json();
      if (current !== revision) return;
      if (!response.ok || data.ok !== true) {
        feedback.textContent = typeof data.detail === 'string' ? data.detail : '在线查找未完成，请刷新后重试。'; return;
      }
      items.append(node('p', '本次检索词：' + (data.query || actualQuery), 'helper'));
      for (const card of data.cards || []) {
        const article = node('article', undefined, 'online-result');
        const named = card.contest_name && card.contest_name !== '待核验赛事';
        article.append(node('h3', named ? card.contest_name : '来源线索：' + (card.raw_title || '来源未提供标题')));
        if (!named) article.append(node('p', '具体赛事名称尚待核对。', 'helper'));
        article.append(node('p', '未经审核 · ' + (card.status || '未知') + ' · ' + (card.format || '未知'), 'helper'));
        const quality = data.classifications?.[card.source_url];
        if (quality) article.append(node('p', quality.kind + ' · ' + quality.reason, 'helper'));
        article.append(node('p', card.snippet || '来源暂无摘要，请核对原文。', 'helper'));
        if (/^https?:\/\//i.test(card.source_url || '')) {
          const link = node('a', card.source_url, 'source-url helper'); link.href = card.source_url;
          link.target = '_blank'; link.rel = 'noopener noreferrer'; article.append(link);
          if (card.raw_title) article.append(node('p', '来源标题：' + card.raw_title, 'helper'));
        }
        article.append(node('p', '检索时间：' + (card.search_time || '未记录'), 'helper'));
        if (card.published) article.append(node('p', '来源发布时间：' + card.published + '（不代表报名截止）', 'helper'));
        if (card.deadline) article.append(node('p', '截止线索：' + card.deadline + '（待核对）', 'helper'));
        items.append(article);
      }
      if (data.admin_import) {
        feedback.textContent = '保留 ' + data.total + ' 条线索，新增 ' + data.admin_import.imported + ' 条未核验草稿，跳过 ' + data.admin_import.skipped + ' 条既有来源；尚未公开。';
        const link = node('a', '前往管理员未核验 →', 'text-link'); link.href = '/admin#contests'; items.append(link);
      } else feedback.textContent = '保留 ' + data.total + ' 条线索，仅本页可见，未经审核，不进入公共目录。';
      if (data.counts) feedback.textContent = '返回 ' + data.counts.returned + ' 个网页，排除 ' + data.counts.excluded + ' 条非单场赛事页面，去重 ' + data.counts.duplicates + ' 条，忽略 ' + data.counts.invalid + ' 条无效结果。' + feedback.textContent;
      if (data.excluded?.length) {
        const details = node('details', undefined, 'online-excluded');
        details.append(node('summary', '查看排除的网页与原因（' + data.excluded.length + '）'));
        for (const entry of data.excluded) {
          const row = node('article', undefined, 'online-result');
          row.append(node('h3', entry.raw_title || '来源未提供标题'));
          row.append(node('p', entry.kind + ' · ' + entry.reason, 'helper'));
          if (/^https?:\/\//i.test(entry.source_url || '')) {
            const link = node('a', entry.source_url, 'source-url helper'); link.href = entry.source_url;
            link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link);
          }
          row.append(node('p', '检索时间：' + entry.search_time, 'helper')); details.append(row);
        }
        items.append(details);
      }
      if (!data.total) items.append(node('p', '本次没有可收录线索，请查看排除原因或调整条件。', 'helper'));
    } catch (_) {
      if (current !== revision) return;
      feedback.textContent = '在线查找未完成。请检查网络、本地密钥和设备时间，并使用HTTPS或本机地址访问。';
    } finally { if (current === revision) { running = false; button.disabled = false; available(); } }
  });
  window.addEventListener('storage', available);
  queryInput.addEventListener('input', refreshPreview);
  for (const control of [type, site, freshness]) control?.addEventListener('change', refreshPreview);
  window.addEventListener('pageshow', available);
  window.addEventListener('pagehide', () => { revision += 1; controller?.abort(); running = false; button.disabled = false; items.replaceChildren(); panel.hidden = true; feedback.textContent = ''; });
  available();
}
