// Tencent TC3-HMAC-SHA256. SecretKey and derived signing keys stay in this browser.
const encode = value => new TextEncoder().encode(value);
const hex = bytes => Array.from(new Uint8Array(bytes), v => v.toString(16).padStart(2, '0')).join('');
const hash = async value => hex(await crypto.subtle.digest('SHA-256', encode(value)));
const hmac = async (key, value) => {
  const imported = await crypto.subtle.importKey('raw', typeof key === 'string' ? encode(key) : key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', imported, encode(value));
};

export function directedQuery(raw, type = '') {
  const types = { hackathon: '黑客松', essay: '技术征文', developer: '开发者竞赛' };
  let query = raw.trim() || types[type] || 'AI 黑客松 征文 开发者竞赛';
  if (types[type] && !query.includes(types[type])) query += ' ' + types[type];
  if (/征文/.test(query) && !/软件开发|编程|计算机|开发者/.test(query)) query += ' 软件开发 技术实践';
  return query + ' 报名 参赛规则 截止时间';
}

export async function signRequest(query, id, key, timestamp = Math.floor(Date.now() / 1000), options = {}) {
  const freshness = options.freshness || 'm3';
  if (!['d30', 'm3', 'y1'].includes(freshness) || (options.site && !['csdn.net', 'modelscope.cn'].includes(options.site))) throw new Error('invalid_search_options');
  const body = { Query: query, Cnt: 10, Mode: 0, Freshness: freshness };
  if (options.site) body.Site = options.site;
  const payload = JSON.stringify(body);
  const day = new Date(timestamp * 1000).toISOString().slice(0, 10);
  const signedHeaders = 'content-type;host;x-tc-action';
  const canonical = 'POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:wsa.tencentcloudapi.com\nx-tc-action:searchpro\n\n' + signedHeaders + '\n' + await hash(payload);
  const scope = day + '/wsa/tc3_request';
  const toSign = 'TC3-HMAC-SHA256\n' + timestamp + '\n' + scope + '\n' + await hash(canonical);
  const dateKey = await hmac('TC3' + key, day);
  const serviceKey = await hmac(dateKey, 'wsa');
  const signingKey = await hmac(serviceKey, 'tc3_request');
  const signature = hex(await hmac(signingKey, toSign));
  return { payload, timestamp, authorization: 'TC3-HMAC-SHA256 Credential=' + id + '/' + scope + ', SignedHeaders=' + signedHeaders + ', Signature=' + signature };
}
