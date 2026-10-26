"""Forward one browser-signed SearchPro request; never receive SecretKey or load CAM."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import re
import time
import urllib.error
import urllib.request

from . import store, source_intake
from .parse import dedupe, pages_to_cards, _now_iso
from .search_quality import assess

ENDPOINT = 'https://wsa.tencentcloudapi.com/'
CONTENT_TYPE = 'application/json; charset=utf-8'


class OnlineError(ValueError):
    pass


def validate_envelope(envelope, now=None):
    if not isinstance(envelope,dict) or set(envelope) != {'payload','timestamp','authorization'}:
        raise OnlineError('invalid_request')
    payload, stamp, auth = (envelope[k] for k in ('payload','timestamp','authorization'))
    if (not isinstance(payload,str) or len(payload)>4096 or isinstance(stamp,bool)
            or not isinstance(stamp,int) or abs((time.time() if now is None else now)-stamp)>300
            or not isinstance(auth,str) or len(auth)>600):
        raise OnlineError('invalid_request')
    pattern = r'TC3-HMAC-SHA256 Credential=([A-Za-z0-9]{8,128})/(\d{4}-\d{2}-\d{2})/wsa/tc3_request, SignedHeaders=content-type;host;x-tc-action, Signature=[a-f0-9]{64}'
    match = re.fullmatch(pattern,auth)
    if not match or match[2] != datetime.fromtimestamp(stamp,timezone.utc).date().isoformat():
        raise OnlineError('invalid_signature')
    try:
        body=json.loads(payload)
    except (ValueError,TypeError):raise OnlineError('invalid_request') from None
    if (not isinstance(body,dict) or not {'Query','Cnt','Mode','Freshness'} <= set(body)
            or set(body)-{'Query','Cnt','Mode','Freshness','Site'}
            or not isinstance(body['Query'],str) or not 1<=len(body['Query'].strip())<=300
            or body['Cnt']!=10 or isinstance(body['Cnt'],bool)
            or body['Mode']!=0 or isinstance(body['Mode'],bool) or not isinstance(body['Freshness'],str) or body['Freshness'] not in {'d30','m3','y1'}
            or ('Site' in body and (not isinstance(body['Site'],str) or body['Site'] not in {'csdn.net','modelscope.cn'}))
            or match[1] in body['Query']):
        raise OnlineError('invalid_query')
    return payload,stamp,auth,match[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def upstream(payload,stamp,auth):
    request=urllib.request.Request(ENDPOINT,payload.encode(),{
        'Content-Type':CONTENT_TYPE,'Authorization':auth,'X-TC-Action':'SearchPro',
        'X-TC-Version':'2025-05-08','X-TC-Timestamp':str(stamp)},method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=35) as response:
            raw=response.read(2_000_001)
        if len(raw)>2_000_000:raise OnlineError('invalid_response')
        data=json.loads(raw)
    except OnlineError:raise
    except Exception:raise OnlineError('search_unavailable') from None
    data=data.get('Response') if isinstance(data,dict) else None
    if not isinstance(data,dict):raise OnlineError('invalid_response')
    if data.get('Error'):
        code=data['Error'].get('Code','') if isinstance(data['Error'],dict) else ''
        raise OnlineError('search_auth_failed' if code.startswith(('AuthFailure','UnauthorizedOperation'))
                          else 'search_quota' if code.startswith(('LimitExceeded','RequestLimitExceeded','ResourceUnavailable'))
                          else 'search_unavailable')
    # SearchPro also returns Pages=null for a successful search with no pages.
    if 'Pages' in data and data['Pages'] is None:data['Pages']=[]
    if not isinstance(data.get('Pages'),list):raise OnlineError('invalid_response')
    return data


def search_results(envelope):
    payload,stamp,auth,secret_id=validate_envelope(envelope)
    data=upstream(payload,stamp,auth)
    pages=[]; excluded=[]; classifications={}; invalid=0
    search_time=_now_iso()
    for raw in data['Pages'][:10]:
        try:entry=json.loads(raw) if isinstance(raw,str) else raw
        except ValueError:invalid+=1;continue
        if not isinstance(entry,dict):invalid+=1;continue
        score=entry.get('score',0)
        # Do not echo a provider-supplied credential or arbitrary transport fields.
        entry={key:(value.replace(auth,'[凭据已隐藏]').replace(secret_id,'[凭据已隐藏]')[:limit]
                    if isinstance(value:=entry.get(key,''),str) else '')
               for key,limit in [('title',300),('url',2048),('passage',6000),('content',6000),('site',200),('date',100)]}
        try:source_intake.source_url(entry['url'])
        except ValueError:invalid+=1;continue
        try:score=float(score) if isinstance(score,(int,float)) and not isinstance(score,bool) else 0
        except (ValueError,OverflowError):score=0
        entry['score']=score if math.isfinite(score) and 0<=score<=1 else 0
        quality=assess(entry)
        if quality['retain']:
            pages.append(entry)
            classifications[entry['url']]=quality
        else:
            excluded.append({'raw_title':entry['title'],'source_url':entry['url'],
                             'search_time':search_time,**quality})
    cards=dedupe(pages_to_cards([{'Query':json.loads(payload)['Query'],'Pages':pages}],search_time=search_time))
    cards.sort(key=lambda c:c.score,reverse=True)
    for card in cards:
        card.id=store.stable_id(card)
        card.publication_state='draft';card.record_origin='admin_online_search'
        card.luna_state='pending'
        quality=classifications[card.source_url]
        card.verification_notes.append('在线线索分类：'+quality['kind']+'。'+quality['reason'])
        if quality['kind']=='赛后报道':card.status='已结束'
        card.uncertain_fields=[key for key in ('deadline','host','signup_url') if not getattr(card,key)]
    request_id=data.get('RequestId','')
    request_id=request_id if isinstance(request_id,str) and re.fullmatch(r'[a-fA-F0-9-]{16,64}',request_id) else ''
    return {'cards':cards,'excluded':excluded,'classifications':classifications,
            'search_time':search_time,'query':json.loads(payload)['Query'],'request_id':request_id,
            'counts':{'returned':len(data['Pages'][:10]),'retained':len(cards),
                      'excluded':len(excluded),'invalid':invalid,'duplicates':len(pages)-len(cards)}}


def search(envelope):
    """Card-only compatibility for scripts; API also exposes triage accounting."""
    return search_results(envelope)['cards']


def import_admin_results(cards,path=None):
    """Pending private drafts only; duplicate/retired rows never change."""
    imported,skipped=[],0
    with closing(store._connect(path)) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        existing={store._source_identity(r[0]) for r in conn.execute('SELECT source_url FROM contests')}
        ids={r[0] for r in conn.execute('SELECT id FROM contests')}
        for card in cards:
            if store._source_identity(card.source_url) in existing or card.id in ids:
                skipped+=1;continue
            store._write(conn,card)
            imported.append(card.id);existing.add(store._source_identity(card.source_url));ids.add(card.id)
    return {'imported_ids':imported,'imported':len(imported),'skipped':skipped}
