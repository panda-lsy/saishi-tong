'use strict';
// Progressive enhancement of local GET filters; credentials stay in settings.
(() => {
  const dropdown = document.getElementById('settings-dropdown');
  if (dropdown) {
    document.addEventListener('click', (event) => { if (!dropdown.contains(event.target)) dropdown.open = false; });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && dropdown.open) { dropdown.open = false; dropdown.querySelector('summary')?.focus(); }
    });
  }
  const filters = document.getElementById('directory-filters');
  if (filters) filters.addEventListener('change', (event) => {
    const target = event.target;
    if (!target.matches('input[type="checkbox"]') || !['status', 'format', 'location'].includes(target.name)) return;
    const group = Array.from(filters.querySelectorAll('input[name="' + target.name + '"]'));
    if (!target.value && target.checked) group.forEach(input => { if (input.value) input.checked = false; });
    const any = group.some(input => input.value && input.checked);
    group.forEach(input => { if (!input.value) input.checked = !any; });
    if (target.name === 'format' && !Array.from(filters.querySelectorAll('input[name="format"]')).some(input => input.checked && ['线下', '混合'].includes(input.value))) {
      Array.from(filters.querySelectorAll('input[name="location"]')).forEach(input => { input.checked = !input.value; });
    }
    filters.requestSubmit();
  });
  const settings = document.getElementById('settings-form');
  if (!settings) return;
  const ID = 'TENCENTCLOUD_SECRET_ID';
  const KEY = 'TENCENTCLOUD_SECRET_KEY';
  const LEGACY_USE = 'saishi_use_local_credentials';
  const id = document.getElementById('secret-id');
  const key = document.getElementById('secret-key');
  const status = document.getElementById('settings-feedback');
  const message = (value) => { status.textContent = value; };
  const refresh = () => {
    try {
      localStorage.removeItem(LEGACY_USE);
      id.value = localStorage.getItem(ID) || '';
      key.value = localStorage.getItem(KEY) || '';
      message(id.value && key.value ? '已读取本浏览器保存的密钥；仅“在线查找”会在本机使用SecretKey签名，不上传SecretKey。' : '尚未保存完整本地密钥；不会读取服务器密钥。');
    } catch (_) { message('浏览器禁止本地存储，无法保存或读取设置。'); }
  };
  refresh();
  settings.addEventListener('submit', (event) => {
    event.preventDefault();
    const a = id.value.trim(); const b = key.value.trim();
    if (!a || !b) { message('请同时填写 SecretId 与 SecretKey，或使用一键清除。'); return; }
    try {
      localStorage.setItem(ID, a); localStorage.setItem(KEY, b); localStorage.removeItem(LEGACY_USE);
      message('已仅保存到本浏览器；仅显式点击“在线查找”时在本机签名；本地搜索不使用，也未配置CLI。');
    } catch (_) { message('保存失败：请检查本地存储权限，部分内容可能未写入。'); }
  });
  document.getElementById('refresh-credentials').addEventListener('click', refresh);
  document.getElementById('clear-credentials').addEventListener('click', () => {
    id.value = ''; key.value = '';
    try {
      localStorage.removeItem(ID); localStorage.removeItem(KEY); localStorage.removeItem(LEGACY_USE);
      message('已清除本浏览器保存的密钥与旧使用开关。');
    } catch (_) { message('无法清除本地存储，请通过浏览器设置清理本站数据。'); }
  });
})();
