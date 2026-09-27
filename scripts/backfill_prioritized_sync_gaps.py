#!/usr/bin/env python3
"""実測14件のZoho値を空のNotion項目にだけ補完。既定はdry-run。

入力はinventory調査の差分JSON。ZohoとNotionを再取得し、計画のハッシュ照合後にも
1件ずつ両側を再確認する。Notionの既存非空値は上書きしない。Notion APIに条件付き
更新がないため、最終確認と更新の間の同時編集は完全には排除できない。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from src.audit_log.actor_context import set_actor
from src.db_schema.base import Tool
from src.sync_engine.notion_id_mapping import NotionIdMappingStore
from src.sync_engine.clients.zoho_client import HttpZohoClient
from src.sync_engine.production_wiring import build_notion_clients_by_db
from src.sync_engine.webhook_handlers.zoho_field_transforms import _next_action_date, SKIP_FIELD

TARGETS={('project','次回アクション日'):('Deals','field31',_next_action_date),('chain','その他'):('CustomModule3','field',lambda v:v or None)}


def source_value(client, item):
    module,field,convert=TARGETS[(item['db'],item['property'])]
    if not str(item['external_id']).isdigit():raise ValueError('外部IDが不正')
    response=client.request('GET',f"https://www.zohoapis.jp/crm/v3/{module}/{item['external_id']}?fields={field}",idempotent=True)
    if response.status_code!=200:raise RuntimeError('Zoho取得失敗')
    return convert(response.json()['data'][0].get(field))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',default='migration_output/gap_comparison.json')
    p.add_argument('--mapping-token-env',default='SYNC_ID_MAPPING_NOTION_API_KEY')
    p.add_argument('--apply',action='store_true')
    p.add_argument('--expected-plan-sha256')
    args=p.parse_args();load_dotenv('config/.env');logging.disable(logging.WARNING)
    if args.apply and not args.expected_plan_sha256:raise ValueError('計画ハッシュが必要')
    client=HttpZohoClient(accounts_base_url='https://accounts.zoho.jp')
    notion=build_notion_clients_by_db()
    store=NotionIdMappingStore(api_key=os.environ[args.mapping_token_env])
    def mapping_matches(item):
        mapping=store.find_by_external_id(Tool.ZOHO,item['external_id'],db_key=item['db'])
        return mapping is not None and mapping.notion_key.replace('-','').lower()==item['notion_key'].replace('-','').lower()
    items=json.loads(Path(args.input).read_text())['differences'];plan=[]
    for item in items:
        if item['tool']!='zoho' or item['classification']!='notion_empty' or (item['db'],item['property']) not in TARGETS:continue
        if not mapping_matches(item):continue
        value=source_value(client,item)
        if value is SKIP_FIELD or value in (None,''):continue
        current=notion[item['db']].get_page(item['notion_key'])
        if current is None or current.get(item['property']) not in (None,''):continue
        plan.append({**item,'value':value})
    digest=hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    print(json.dumps({'mode':'apply' if args.apply else 'dry-run','count':len(plan),'plan_sha256':digest},ensure_ascii=False),flush=True)
    if not args.apply:return
    if digest!=args.expected_plan_sha256:raise ValueError('確認時の計画から変わったため中止')
    updated=0;skipped=0
    for item in plan:
        if not mapping_matches(item):skipped+=1;continue
        target=notion[item['db']]
        if source_value(client,item)!=item['value']:skipped+=1;continue
        current=target.get_page(item['notion_key'])
        if current is None or current.get(item['property']) not in (None,''):skipped+=1;continue
        print(json.dumps({'verified_so_far':updated,'skipped':skipped,'next_index':updated+skipped+1}),flush=True)
        with set_actor(source='manual_backfill',label='同期漏れ3項目の初期補完'):
            target.update_page(item['notion_key'],{item['property']:item['value']})
        print(json.dumps({'write_sent':updated+1,'readback_pending':True}),flush=True)
        after=target.get_page(item['notion_key'])
        if after is None or after.get(item['property'])!=item['value']:raise RuntimeError('補完後の読戻し不一致')
        updated+=1
    print(json.dumps({'updated_and_verified':updated,'skipped_due_to_change':skipped}),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('補完停止: '+type(exc).__name__,file=sys.stderr);sys.exit(1)
