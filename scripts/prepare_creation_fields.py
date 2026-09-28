#!/usr/bin/env python3
"""外部登録の専用欄を追加する差分だけを作る。既定は読み取りも書込みもしない。"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.setup_notion_databases import build_property_payload
from src.db_schema.registry import get_schema
from src.sync_review.domain import snapshot_hash

FIELDS = {
    'contact': ('姓', '名'),
    'project': ('完了予定日', '契約予定日', '作成日', '新規・既存種別', 'リードソース1'),
    'product': ('商品カテゴリー', '先方担当者'),
}


def desired_properties(db_key):
    schema = get_schema(db_key)
    result = {}
    for name in FIELDS[db_key]:
        prop = schema.get_property(name)
        result[name] = ({'multi_select': {'options': [{'name': value} for value in prop.options]}}
                        if prop.property_type.value == 'multi_select' else build_property_payload(prop))
    return result


def additive_plan(db_key, existing):
    result = {}
    for name, desired in desired_properties(db_key).items():
        if name not in existing:
            result[name] = desired
        elif existing[name].get('type') != next(iter(desired)):
            raise ValueError('既存項目の型が異なります: ' + name)
        elif 'relation' in desired:
            current_target = str(existing[name].get('relation', {}).get('database_id') or '').replace('-', '')
            if current_target != desired['relation']['database_id'].replace('-', ''):
                raise ValueError('既存の関連先DBが異なります: ' + name)
        elif next(iter(desired)) in {'select', 'multi_select'}:
            kind = next(iter(desired))
            current_options = {item.get('name') for item in existing[name].get(kind, {}).get('options', [])}
            if not {item['name'] for item in desired[kind]['options']} <= current_options:
                raise ValueError('既存項目の選択肢が不足しています: ' + name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', action='store_true', help='実schemaとシート見出しの読み取りだけを行う')
    parser.add_argument('--apply', action='store_true', help='表示版が同じ時だけ不足欄を追加する')
    parser.add_argument('--expected-hash')
    args = parser.parse_args()
    if not args.inspect and not args.apply:
        print(json.dumps({db:desired_properties(db) for db in FIELDS}, ensure_ascii=False, indent=2))
        return
    from src.sync_engine.production_wiring import build_notion_clients_by_db, build_spreadsheet_targets_by_db
    from src.sync_engine.clients._http import raise_for_error, ApiError
    notion = build_notion_clients_by_db(max_rate_limit_retries=3)
    sheets = build_spreadsheet_targets_by_db()
    plan = {}
    for db_key, names in FIELDS.items():
        schema = get_schema(db_key)
        response = notion[db_key]._request('GET', '/databases/' + schema.notion_database_id)
        raise_for_error(response, ApiError)
        existing = response.json()['properties']
        headers = sheets[db_key]._client._get_header_row(schema.spreadsheet_sheet_name)
        if len(headers) != len(set(headers)):
            raise ValueError('シートの見出しが重複しています: ' + db_key)
        plan[db_key] = {'notion': additive_plan(db_key, existing),
                        'sheet': [name for name in names if name not in headers], 'headers': headers,
                        'existing': {name: existing[name] for name in names if name in existing}}
    fingerprint = snapshot_hash(plan)
    print(json.dumps({'hash':fingerprint, 'plan':plan}, ensure_ascii=False, indent=2))
    if not args.apply:
        return
    if args.expected_hash != fingerprint:
        raise ValueError('追加対象が変わりました。inspectの結果を確認してください')
    for db_key, changes in plan.items():
        schema = get_schema(db_key)
        if changes['notion']:
            response = notion[db_key]._request('PATCH', '/databases/' + schema.notion_database_id,
                json_body={'properties':changes['notion']})
            raise_for_error(response, ApiError)
        for name in changes['sheet']:
            sheets[db_key]._client.ensure_sync_key_column(schema.spreadsheet_sheet_name, name)
    print('専用欄の追加処理が完了しました。inspectで実物を読み戻してください。')


if __name__ == '__main__':
    main()
