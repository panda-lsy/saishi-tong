"""Explicit one-call SearchPro evaluation for your own offline CLI credentials."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from saishi_tong.agent_tools import _load_cam
from saishi_tong.client import get_client
from saishi_tong.parse import pages_to_cards
from saishi_tong.search_quality import assess

def main():
    ap=argparse.ArgumentParser(description='One real offline SearchPro request; uses your quota, never writes catalog')
    ap.add_argument('query');ap.add_argument('--site',choices=['csdn.net','blog.csdn.net','modelscope.cn'])
    ap.add_argument('--freshness',choices=['d30','m3','y1'],default='m3');args=ap.parse_args()
    _load_cam()
    from tencentcloud.wsa.v20250508 import models
    body={'Query':args.query,'Cnt':10,'Mode':0,'Freshness':args.freshness}
    if args.site:body['Site']=args.site
    request=models.SearchProRequest();request.from_json_string(json.dumps(body,ensure_ascii=False))
    try:raw=json.loads(get_client().SearchPro(request).to_json_string())
    except Exception:raise SystemExit('SearchPro unavailable; check your own credentials/permissions/quota. No retry performed.') from None
    results=[]
    for value in raw.get('Pages') or []:
        try:page=json.loads(value) if isinstance(value,str) else value
        except ValueError:continue
        if not isinstance(page,dict):continue
        page={key:page.get(key,'') if isinstance(page.get(key,''),str) else '' for key in ('title','url','passage','content')}
        quality=assess(page)
        card=pages_to_cards([{'Query':args.query,'Pages':[page]}])[0] if quality['retain'] and (page['title'] or page['url']) else None
        results.append({'title':page['title'],'source_url':page['url'],**quality,
                        'deadline':card.deadline if card else '', 'host':card.host if card else ''})
    print(json.dumps({'request_id':raw.get('RequestId',''),'query':args.query,'returned':len(results),
                      'retained':sum(r['retain'] for r in results),'results':results},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
