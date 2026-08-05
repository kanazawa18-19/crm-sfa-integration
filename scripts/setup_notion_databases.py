#!/usr/bin/env python3
"""src/db_schema/ の定義から Notion API 経由で6DBを自動作成するセットアップスクリプト。

使い方:
    python scripts/setup_notion_databases.py --dry-run   # 作成予定の構造を表示するだけ
    python scripts/setup_notion_databases.py             # 実際にNotion APIでDBを作成する

実行には環境変数 NOTION_API_KEY / NOTION_PARENT_PAGE_ID が必要（config/.env参照）。
--dry-run 時はAPIキーが無くても構造確認ができる。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.base import DatabaseSchema, PropertyDefinition, PropertyType
from src.db_schema.registry import ALL_SCHEMAS

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"
_REQUEST_TIMEOUT_SECONDS = 30

# create_all_databases が途中失敗しても再実行で重複作成しないよう、
# 作成済みDB IDを都度書き出しておくキャッシュファイル。
_DB_IDS_CACHE_PATH = Path(__file__).resolve().parent / ".notion_db_ids.json"

# Notion API の型へのマッピング。
# NOTE: STATUS型はNotion APIからの新規作成に未対応（2022-06-28時点、UI操作でのみ作成可）のため
#       select にフォールバックする。運用時はNotion UI側でstatusへ手動変換する想定。
_PROPERTY_TYPE_TO_NOTION: dict[PropertyType, str] = {
    PropertyType.TITLE: "title",
    PropertyType.TEXT: "rich_text",
    PropertyType.SELECT: "select",
    PropertyType.STATUS: "select",
    PropertyType.RELATION: "relation",
    PropertyType.NUMBER: "number",
    PropertyType.CURRENCY: "number",
    PropertyType.DATE: "date",
    PropertyType.DATETIME: "date",
    PropertyType.EMAIL: "email",
    PropertyType.PHONE: "phone_number",
    PropertyType.URL: "url",
    PropertyType.CHECKBOX: "checkbox",
    PropertyType.USER: "people",
    PropertyType.JSON_TEXT: "rich_text",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Notion APIを呼び出さず、作成予定のDB構造を表示するだけ",
    )
    return parser.parse_args(argv)


def build_property_payload(
    prop: PropertyDefinition, created_db_ids: dict[str, str]
) -> dict[str, Any]:
    """PropertyDefinition を Notion API の properties ペイロード片へ変換する。"""
    if prop.property_type == PropertyType.TITLE:
        return {"title": {}}

    if prop.property_type in (PropertyType.SELECT, PropertyType.STATUS):
        return {"select": {"options": [{"name": option} for option in prop.options]}}

    if prop.property_type == PropertyType.RELATION:
        assert prop.relation_target is not None
        target_db_id = created_db_ids.get(prop.relation_target)
        if target_db_id is None:
            raise RuntimeError(
                f"relation property '{prop.name}' の参照先DB "
                f"'{prop.relation_target}' がまだ作成されていません"
            )
        # dual_property: 参照先DB側にも逆参照プロパティを自動生成させる。
        # プロパティ名はNotion側が自動採番するため、既存プロパティ名との衝突は起きない。
        return {"relation": {"database_id": target_db_id, "dual_property": {}}}

    if prop.property_type == PropertyType.CURRENCY:
        return {"number": {"format": "yen"}}

    if prop.property_type == PropertyType.NUMBER:
        return {"number": {"format": "number"}}

    notion_type = _PROPERTY_TYPE_TO_NOTION[prop.property_type]
    return {notion_type: {}}


def build_create_database_payload(schema: DatabaseSchema, parent_page_id: str) -> dict[str, Any]:
    """初回作成分のペイロード（RELATION型プロパティは含めない。第2パスでPATCHする）。"""
    properties: dict[str, Any] = {}
    for prop in schema.properties:
        if prop.property_type == PropertyType.RELATION:
            continue
        properties[prop.name] = build_property_payload(prop, created_db_ids={})

    return {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": schema.display_name}}],
        "properties": properties,
    }


def build_relation_patch_payload(
    schema: DatabaseSchema, created_db_ids: dict[str, str]
) -> dict[str, Any]:
    """RELATION型プロパティのみを対象にした2パス目のPATCHペイロード。

    リレーションは参照先DBが実在しないと作成できないため、
    全DBを先に作り終えてから改めて関連付けを行う。
    """
    properties: dict[str, Any] = {}
    for prop in schema.properties:
        if prop.property_type != PropertyType.RELATION:
            continue
        properties[prop.name] = build_property_payload(prop, created_db_ids=created_db_ids)
    return {"properties": properties}


def print_dry_run_plan() -> None:
    print("=== dry-run: 作成予定のNotion DB構造 ===\n")
    for schema in ALL_SCHEMAS:
        print(f"[{schema.display_name}] (key={schema.key}, prefix={schema.id_prefix})")
        for prop in schema.properties:
            relation_note = f" -> {prop.relation_target}" if prop.relation_target else ""
            print(
                f"  - {prop.name:<20} type={prop.property_type.value:<10} "
                f"requirement={prop.requirement.value:<8} sync_scope={prop.sync_scope.value}"
                f"{relation_note}"
            )
            if prop.property_type == PropertyType.STATUS:
                print(
                    "      ※Notion APIの制約によりselect型として作成されます"
                    "（運用時はNotion UI側でstatusへ手動変換）"
                )
        print()


def _notion_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _load_cached_db_ids() -> dict[str, str]:
    if not _DB_IDS_CACHE_PATH.exists():
        return {}
    return json.loads(_DB_IDS_CACHE_PATH.read_text(encoding="utf-8"))


def _save_cached_db_ids(created_db_ids: dict[str, str]) -> None:
    _DB_IDS_CACHE_PATH.write_text(
        json.dumps(created_db_ids, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def create_all_databases(api_key: str, parent_page_id: str) -> dict[str, str]:
    import requests

    headers = _notion_headers(api_key)
    # 途中失敗しての再実行に備え、前回までに作成済みのDB IDをキャッシュから復元する。
    created_db_ids: dict[str, str] = _load_cached_db_ids()

    for schema in ALL_SCHEMAS:
        if schema.key in created_db_ids:
            print(f"skip (already created): {schema.display_name} -> {created_db_ids[schema.key]}")
            continue
        payload = build_create_database_payload(schema, parent_page_id)
        response = requests.post(
            f"{NOTION_API_BASE}/databases",
            headers=headers,
            json=payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        db_id = response.json()["id"]
        created_db_ids[schema.key] = db_id
        _save_cached_db_ids(created_db_ids)
        print(f"created: {schema.display_name} -> {db_id}")

    for schema in ALL_SCHEMAS:
        patch_payload = build_relation_patch_payload(schema, created_db_ids)
        if not patch_payload["properties"]:
            continue
        db_id = created_db_ids[schema.key]
        response = requests.patch(
            f"{NOTION_API_BASE}/databases/{db_id}",
            headers=headers,
            json=patch_payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        print(f"linked relations: {schema.display_name}")

    return created_db_ids


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.dry_run:
        print_dry_run_plan()
        return

    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        print(
            "ERROR: 環境変数 NOTION_API_KEY が設定されていません。"
            " config/.env に NOTION_API_KEY を設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")
    if not parent_page_id:
        print(
            "ERROR: 環境変数 NOTION_PARENT_PAGE_ID が設定されていません。"
            " DB作成先の親ページIDを config/.env に設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    created_db_ids = create_all_databases(api_key, parent_page_id)
    print("\n作成完了:")
    for key, db_id in created_db_ids.items():
        print(f"  {key}: {db_id}")


if __name__ == "__main__":
    main()
