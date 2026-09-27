#!/usr/bin/env python3
"""Notion・シートの対応表未登録を読み取り専用で一覧化する。

業務APIへの変更呼出はない。Notionの1万件制限は既存キーセット取得で越える。
出力はID・行番号だけ（顧客項目の値を保存しない）。ローカルへのみJSONを保存する。
同時更新があるためシステム間の厳密な同一時点スナップショットではない。
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from dotenv import load_dotenv
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.clients._notion_paging import query_all_with_keyset
from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient
from src.sync_engine.notion_id_mapping import NotionIdMappingStore


class InventoryError(RuntimeError):
    """秘密値を含まない、運用者へ表示できる調査エラー。"""


def norm(value):
    return str(value or '').strip().replace('-', '').lower()


def classify(pages, mappings, rows):
    """移動・重複したシート行を新規と取り違えないため同期キーも照合する。"""
    if any(not norm(m['notion_key']) for m in mappings):
        raise InventoryError('対応表に空のNotionキーがあります')
    mapped = {norm(m['notion_key']): m for m in mappings}
    by_row = {m['spreadsheet_row']: m for m in mappings if m['spreadsheet_row'] is not None}
    page_keys = {norm(p['id']) for p in pages}
    key_counts = Counter(norm(r['key']) for r in rows if r['key'])
    mapping_keys = Counter(norm(m['notion_key']) for m in mappings)
    mapping_rows = Counter(m['spreadsheet_row'] for m in mappings if m['spreadsheet_row'] is not None)
    entries = []
    for row in rows:
        key = norm(row['key'])
        mapping = by_row.get(row['row'])
        if mapping_rows[row['row']] > 1 or (key and mapping_keys[key] > 1): kind = 'duplicate_mapping'
        elif key and key_counts[key] > 1: kind = 'duplicate_sync_key'
        elif key and key not in page_keys: kind = 'sync_key_without_page'
        elif mapping and key and key != norm(mapping['notion_key']): kind = 'row_mapping_mismatch'
        elif mapping and not key: kind = 'mapped_row_without_sync_key'
        elif mapping: kind = 'mapped'
        elif key in mapped and mapped[key]['spreadsheet_row'] is None: kind = 'mapped_key_without_row_index'
        elif key in mapped: kind = 'mapped_key_at_other_row'
        else: kind = 'unmapped'
        entries.append({**row, 'classification': kind})
    missing = [p for p in pages if norm(p['id']) not in mapped]
    return {'notion_total':len(pages), 'mapping_total':len(mappings), 'notion_unmapped':len(missing),
            'sheet_nonempty_rows':len(rows), 'sheet_classes':dict(Counter(r['classification'] for r in entries)),
            'notion_unmapped_pages':missing, 'sheet_review_rows':[r for r in entries if r['classification']!='mapped'],
            'duplicate_mapping_keys':sum(n>1 for n in mapping_keys.values()),
            'duplicate_mapping_rows':sum(n>1 for n in mapping_rows.values()),
            'mapped_without_sheet_row':sum(m['spreadsheet_row'] is None for m in mappings),
            'mapped_without_kintone':sum(not m.get('kintone_id') for m in mappings),
            'mapped_without_zoho':sum(not m.get('zoho_id') for m in mappings)}


def fetch_pages(schema, checkpoint=None, api_key=None, quiet=False):
    session = requests.Session()
    session.headers.update({'Authorization':'Bearer '+(api_key or os.environ['NOTION_API_KEY']),'Notion-Version':'2022-06-28'})
    requests_count = 0
    cached = {}
    if checkpoint and checkpoint.exists():
        for line in checkpoint.read_text().splitlines():
            for page in json.loads(line): cached[page['id']] = page
    watermark = max((p['created_time'] for p in cached.values()), default=None)
    def post(body):
        nonlocal requests_count
        # filter_propertiesで項目値を省き、識別子・日時だけを残す。
        for attempt in range(6):
            try:
                response = session.post(f'https://api.notion.com/v1/databases/{schema.notion_database_id}/query',
                                        params={'filter_properties':'title'},json=body,timeout=30)
            except (requests.Timeout, requests.ConnectionError):
                time.sleep(min(30, 2 ** attempt))
                continue
            if response.status_code >= 500:
                time.sleep(min(30, 2 ** attempt))
                continue
            if response.status_code==429:
                time.sleep(min(30,max(1,float(response.headers.get('Retry-After','2')))))
                continue
            if response.status_code!=200: raise InventoryError(f'Notion query HTTP {response.status_code}')
            result=response.json()
            result['results']=[{k:p[k] for k in ('id','created_time','last_edited_time')} for p in result['results']]
            if checkpoint:
                with checkpoint.open('a') as handle: handle.write(json.dumps(result['results'])+'\n')
            requests_count+=1
            if requests_count%50==0 and not quiet: print(f'{schema.key}: Notion {requests_count} requests',flush=True)
            return result
        raise InventoryError('Notion rate limit')
    base_filter={'timestamp':'created_time','created_time':{'on_or_after':watermark}} if watermark else None
    pages=query_all_with_keyset(post,label=schema.key,base_filter=base_filter)
    cached.update({p['id']:p for p in pages})
    return list(cached.values())


def fetch_rows(client, sheet):
    escaped=sheet.replace("'", "''")
    meta=client._request('GET','',params={'fields':'sheets(properties(title,gridProperties(rowCount,columnCount)))'})
    if meta.status_code!=200: raise InventoryError('Sheets metadata failed')
    grid=next((x['properties']['gridProperties'] for x in meta.json()['sheets'] if x['properties']['title']==sheet), None)
    if grid is None: raise InventoryError('対象シートが見つかりません')
    from src.sync_engine.clients.spreadsheet_client import column_letter
    last=column_letter(grid['columnCount'])
    rows=[]; key_index=None
    for start in range(1,grid['rowCount']+1,5000):
        rng=f"'{escaped}'!A{start}:{last}{min(start+4999,grid['rowCount'])}"
        response=client._request('GET','/values/'+quote(rng,safe=''),params={'valueRenderOption':'UNFORMATTED_VALUE'})
        if response.status_code!=200: raise InventoryError(f'Sheets values HTTP {response.status_code}')
        values=response.json().get('values',[])
        if start==1:
            headers=values[0] if values else []
            if headers.count('同期キー')!=1: raise InventoryError('同期キー列が無いか重複しています')
            key_index=headers.index('同期キー') if '同期キー' in headers else None
        for offset,cells in enumerate(values):
            row=start+offset
            if row==1 or not any(c is not None and str(c).strip() for c in cells): continue
            key=str(cells[key_index]).strip() if key_index is not None and key_index<len(cells) else ''
            rows.append({'row':row,'key':key})
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume',action='store_true',help='同じ出力先の読み取りチェックポイントから再開')
    parser.add_argument('--notion-token-env',default='NOTION_API_KEY')
    parser.add_argument('--quiet',action='store_true',help='集計結果を標準出力へ出さない')
    parser.add_argument('--db',choices=[s.key for s in ALL_SCHEMAS],action='append')
    parser.add_argument('--mapping-token-env', default='SYNC_ID_MAPPING_NOTION_API_KEY')
    parser.add_argument('--output-dir',default='migration_output/hub_inventory/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args=parser.parse_args();load_dotenv('config/.env')
    if args.quiet: logging.disable(logging.CRITICAL)
    store=NotionIdMappingStore(api_key=os.environ[args.mapping_token_env],max_rate_limit_retries=5)
    client=HttpSpreadsheetClient()
    output=Path(args.output_dir);output.mkdir(parents=True,exist_ok=True)
    for schema in ALL_SCHEMAS:
        if args.db and schema.key not in args.db: continue
        if not args.quiet:print(f'{schema.key}: 読取開始',flush=True)
        mapping_path=output/f'.{schema.key}.mappings.json'
        checkpoint=output/f'.{schema.key}.pages.jsonl'
        if checkpoint.exists() and not args.resume: raise InventoryError('既存チェックポイントあり。別出力先または--resumeを指定')
        if args.resume and mapping_path.exists():
            mappings=json.loads(mapping_path.read_text())
        else:
            mappings=[asdict(m) for m in store.list_by_db(schema.key)]
            mapping_path.write_text(json.dumps(mappings,default=str))
        if not args.quiet:print(f'{schema.key}: 対応表 {len(mappings)}',flush=True)
        pages=fetch_pages(schema,checkpoint,os.environ[args.notion_token_env],quiet=args.quiet)
        rows=fetch_rows(client,schema.spreadsheet_sheet_name)
        result={'observed_at':datetime.now(timezone.utc).isoformat(),'db':schema.key,**classify(pages,mappings,rows)}
        (output/f'{schema.key}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        if not args.quiet:
            print(json.dumps({k:v for k,v in result.items() if k not in ('notion_unmapped_pages','sheet_review_rows')},ensure_ascii=False),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # 生のHTTP例外には認証情報が含まれ得るので種類だけを表示する。
        print('調査中断: '+(str(exc) if isinstance(exc,InventoryError) else type(exc).__name__),file=sys.stderr);sys.exit(1)
