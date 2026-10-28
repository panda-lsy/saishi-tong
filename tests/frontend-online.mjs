import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { signRequest, directedQuery } from '../saishi_tong/web/static/online-signing.mjs';

// Independently computed with Python hashlib/hmac from the official TC3 spec;
// checks UTF-8 payload, UTC scope and the exact headers sent by the relay.
const signed = await signRequest('中文黑客松', 'AKIDsyntheticTestOnly00000000', 'synthetic-key-only', 1792782000);
assert(signed.authorization.endsWith('Signature=d97293ca7e608f66bff7cb1f4cb5572826af3b1e31753519414ba573eeeca502'));
assert(!JSON.stringify(signed).includes('synthetic-key-only'));
assert.equal(JSON.parse(signed.payload).Query, '中文黑客松');
const directed = await signRequest('征文 报名', 'AKIDsyntheticTestOnly00000000', 'synthetic-key-only', 1792782000, {site:'csdn.net',freshness:'d30'});
assert.deepEqual(JSON.parse(directed.payload),{Query:'征文 报名',Cnt:10,Mode:0,Freshness:'d30',Site:'csdn.net'});
assert.notEqual(directed.authorization,signed.authorization);
const lite = await signRequest('征文 报名', 'AKIDsyntheticTestOnly00000000', 'synthetic-key-only', 1792782000, {site:'csdn.net',freshness:'d30',edition:'lite'});
assert.deepEqual(JSON.parse(lite.payload),{Query:'征文 报名',Freshness:'d30',Site:'csdn.net'});
assert.notEqual(lite.authorization,directed.authorization);
assert.equal(directedQuery('征文','essay'),'征文 技术征文 软件开发 技术实践 报名 参赛规则 截止时间');
assert.equal(directedQuery('文学','essay'),'文学 技术征文 软件开发 技术实践 报名 参赛规则 截止时间');
assert(!directedQuery('文学','essay').includes('AI'));
await assert.rejects(()=>signRequest('test','synthetic','synthetic',1792782000,{site:'evil.test'}));

const script = (await readFile(new URL('../saishi_tong/web/static/online.js', import.meta.url), 'utf8')).replace(/^import .*;\s*/,'');
class Node {
  constructor(){this.children=[];this.listeners={};this.value='';this.hidden=false;this.disabled=false;this.textContent='';}
  addEventListener(k,f){(this.listeners[k] ||= []).push(f);}
  append(v){this.children.push(v);}
  replaceChildren(){this.children=[];}
  async fire(k){await Promise.all((this.listeners[k] || []).map(f=>f()));}
}
function fixture({complete=true,admin=false,fail=false,excluded=false}={}){
  const nodes=Object.fromEntries(['online-find','online-feedback','online-results','online-result-list','online-options','online-type','online-site','online-freshness','online-edition','online-query-preview','q'].map(k=>[k,new Node()]));
  const window=new Node(), requests=[],storage=new Map();
  if(complete){storage.set('TENCENTCLOUD_SECRET_ID','synthetic-id-only');storage.set('TENCENTCLOUD_SECRET_KEY','synthetic-key-only');}
  nodes.q.value='中文黑客松';
  const signedOptions=[];
  const context={document:{getElementById:k=>nodes[k],createElement:()=>new Node()},window,AbortController,directedQuery,
    localStorage:{getItem:k=>storage.get(k),setItem(){throw Error('no result or credential writes');}},
    signRequest:async(q,id,key,stamp,options)=>{assert.equal(id,'synthetic-id-only');assert.equal(key,'synthetic-key-only');signedOptions.push({q,...options});return{payload:q,timestamp:1,authorization:'signed-envelope-only'};},
    fetch:async(path,options)=>{
      requests.push({path,options});assert.equal(options.credentials,'same-origin');
      assert(!JSON.stringify(options).includes('synthetic-key-only'));
      if(path==='/api/admin/session')return{json:async()=>({authenticated:admin,csrf_token:admin?'synthetic-csrf':undefined})};
      if(fail)return{ok:false,json:async()=>({detail:'搜索额度受限'})};
      return{ok:true,json:async()=>({ok:true,total:1,admin_import:admin?{imported:1,skipped:0}:null,
        counts:{returned:excluded?2:1,retained:1,excluded:excluded?1:0,duplicates:0,invalid:0},
        excluded:excluded?[{raw_title:'征文是什么意思',source_url:'https://example.test/meaning',kind:'非赛事',reason:'词语解释',search_time:'2026-10-25'}]:[],
        cards:[{contest_name:'<img onerror=bad>',status:'未知',format:'线上',source_url:'https://example.test/contest',search_time:'2026-10-24T00:00:00Z'}]})};
    }};
  vm.runInNewContext(script,context);return{nodes,window,requests,storage,signedOptions};
}
{
  const s=fixture({complete:false});assert(s.nodes['online-find'].hidden);assert(s.nodes['online-options'].hidden);assert.equal(s.requests.length,0);
}
{
  const s=fixture();assert(!s.nodes['online-find'].hidden);assert.equal(s.requests.length,0);
  await s.nodes['online-find'].fire('click');assert.equal(s.requests.length,2);
  assert(!s.requests[1].options.headers['X-CSRF-Token']);
  assert.match(s.nodes['online-feedback'].textContent,/仅本页可见/);
  const result=s.nodes['online-result-list'].children[1];
  assert.equal(result.children[0].textContent,'<img onerror=bad>');
  assert(result.children.some(c=>c.textContent==='https://example.test/contest'));
  assert(result.children.some(c=>c.textContent.includes('检索时间')));
  await s.window.fire('pagehide');assert.equal(s.nodes['online-result-list'].children.length,0);
}
{
  const s=fixture({excluded:true});s.nodes.q.value='文学';s.nodes['online-type'].value='essay';
  s.nodes['online-site'].value='csdn.net';s.nodes['online-freshness'].value='d30';s.nodes['online-edition'].value='lite';
  await s.nodes['online-type'].fire('change');assert.equal(s.requests.length,0);
  assert.match(s.nodes['online-query-preview'].textContent,/文学 技术征文 软件开发 技术实践 报名/);
  await s.nodes['online-find'].fire('click');
  assert.equal(s.signedOptions[0].site,'csdn.net');assert.equal(s.signedOptions[0].freshness,'d30');assert.equal(s.signedOptions[0].edition,'lite');
  assert.equal(s.requests.filter(r=>r.path==='/api/online-search').length,1);
  assert.match(s.nodes['online-feedback'].textContent,/返回 2 个网页，排除 1/);
  assert(s.nodes['online-result-list'].children.some(c=>c.children.some(v=>v.textContent.includes('查看排除'))));
}
{
  const s=fixture({admin:true});await s.nodes['online-find'].fire('click');
  assert.equal(s.requests[1].options.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.match(s.nodes['online-feedback'].textContent,/新增 1 条未核验草稿/);
  assert.equal(s.nodes['online-result-list'].children.at(-1).href,'/admin#contests');
}
{
  const s=fixture({fail:true});await s.nodes['online-find'].fire('click');
  assert.equal(s.nodes['online-feedback'].textContent,'搜索额度受限');assert(!s.nodes['online-find'].disabled);
  assert.equal(s.nodes.q.value,'中文黑客松');
}
assert(!script.includes('innerHTML'));
console.log('PASS online: independent TC3 signature vector; key gating/no auto-search; private guest/admin draft flows; CSRF; provenance; error/cleanup/XSS');
