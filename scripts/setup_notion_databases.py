#!/usr/bin/env python3
"""src/db_schema/ の連絡先DB・サービス商品DB定義を Notion API 経由で反映するセットアップスクリプト。

既存の稼働中Notionワークスペースには6DB全部が既に存在している。取引先マスター・チェーン・
案件管理・アクション履歴の4DBは実データを保持する稼働中DBのため、本スクリプトは一切変更を
加えない。連絡先DB・サービス商品DBの2つ（作成直後でtitleプロパティ「名前」のみを持つ空DB）
にのみ、src/db_schema/ で定義されたプロパティを `PATCH /v1/databases/{database_id}` で追加
する。新規DB作成（`POST /v1/databases`）は行わない。

使い方:
    python scripts/setup_notion_databases.py --dry-run   # 追加予定のプロパティを表示するだけ
    python scripts/setup_notion_databases.py             # 実際にNotion APIでプロパティを追加する

実行には環境変数 NOTION_API_KEY が必要（config/.env参照）。
--dry-run 時はAPIキーが無くても構造確認ができる。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.base import DatabaseSchema, PropertyDefinition, PropertyType
from src.db_schema.registry import ALL_SCHEMAS, get_schema

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"
_REQUEST_TIMEOUT_SECONDS = 30

# プロパティ追加の対象は、Notion側でtitle「名前」のみを持つ空DBとして最近作成された
# 連絡先DB・サービス商品DBの2つのみ。既存4DB（取引先マスター/チェーン/案件管理/
# アクション履歴）は実データを保持する稼働中DBのため対象に含めない。
TARGET_DB_KEYS: tuple[str, ...] = ("contact", "product")

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
        help="Notion APIを呼び出さず、連絡先DB・サービス商品DBへ追加予定のプロパティを表示するだけ",
    )
    return parser.parse_args(argv)


def build_property_payload(prop: PropertyDefinition) -> dict[str, Any]:
    """PropertyDefinition を Notion API の properties ペイロード片へ変換する。

    RELATION型は参照先DBの notion_database_id を src.db_schema.registry から直接引ける
    ため（既存4DBを含む全DBが既に notion_database_id を持つ）、作成順序を気にする
    2パス構成は不要で、1回の呼び出しで解決できる。
    """
    if prop.property_type == PropertyType.TITLE:
        return {"title": {}}

    if prop.property_type in (PropertyType.SELECT, PropertyType.STATUS):
        return {"select": {"options": [{"name": option} for option in prop.options]}}

    if prop.property_type == PropertyType.RELATION:
        assert prop.relation_target is not None
        target_schema = get_schema(prop.relation_target)
        if target_schema.notion_database_id is None:
            raise RuntimeError(
                f"relation property '{prop.name}' の参照先DB '{prop.relation_target}' に"
                " notion_database_id が設定されていません"
            )
        # single_property: 連絡先DB側からの片方向リレーションのみを作成する。
        # dual_propertyを使うと、Notion API側の自動処理で参照先DB（取引先マスター等の
        # 実データを保持する既存4DB）にも逆参照プロパティが自動生成されてしまい、
        # 「既存4DBには一切変更を加えない」という本スクリプトの前提が崩れるため避けている
        # （shirokuma-secレビュー: BLOCKER）。取引先マスター側から連絡先DBを逆引きしたい
        # 場合は、Notion UI側で手動でリレーションプロパティを追加する運用とする。
        return {"relation": {"database_id": target_schema.notion_database_id, "single_property": {}}}

    if prop.property_type == PropertyType.CURRENCY:
        return {"number": {"format": "yen"}}

    if prop.property_type == PropertyType.NUMBER:
        return {"number": {"format": "number"}}

    notion_type = _PROPERTY_TYPE_TO_NOTION.get(prop.property_type)
    if notion_type is None:
        raise ValueError(
            f"property '{prop.name}' の type={prop.property_type.value!r} は"
            " Notion APIへの追加に未対応の型です（_PROPERTY_TYPE_TO_NOTIONを確認してください）"
        )
    return {notion_type: {}}


def build_update_properties_payload(schema: DatabaseSchema) -> dict[str, Any]:
    """schema.properties から、Notion側に追加すべきプロパティのPATCHペイロードを組み立てる。

    title プロパティ（「名前」）は連絡先DB・サービス商品DBともにNotion側に既に存在する
    ため、重複作成やエラーを避けるためペイロードから除外する。
    """
    properties: dict[str, Any] = {}
    for prop in schema.properties:
        if prop.property_type == PropertyType.TITLE:
            continue
        properties[prop.name] = build_property_payload(prop)
    return {"properties": properties}


def print_dry_run_plan() -> None:
    print("=== dry-run: 連絡先DB・サービス商品DBに追加予定のプロパティ ===\n")
    for schema in ALL_SCHEMAS:
        if schema.key not in TARGET_DB_KEYS:
            print(f"[{schema.display_name}] (key={schema.key}) -> 変更なし（既存DBのため）\n")
            continue

        print(f"[{schema.display_name}] (key={schema.key}, notion_database_id={schema.notion_database_id})")
        for prop in schema.properties:
            if prop.property_type == PropertyType.TITLE:
                print(f"  - {prop.name:<20} type=title       -> 既存のため追加対象外")
                continue
            relation_note = f" -> {prop.relation_target}" if prop.relation_target else ""
            print(
                f"  - {prop.name:<20} type={prop.property_type.value:<10} "
                f"requirement={prop.requirement.value:<8} sync_scope={prop.sync_scope.value}"
                f"{relation_note}"
            )
            if prop.property_type == PropertyType.STATUS:
                print(
                    "      ※Notion APIの制約によりselect型として追加されます"
                    "（運用時はNotion UI側でstatusへ手動変換）"
                )
        print()


def _notion_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def update_target_databases(api_key: str) -> None:
    """連絡先DB・サービス商品DBのみに対して、定義済みプロパティをPATCHで追加する。"""
    import requests

    headers = _notion_headers(api_key)

    for key in TARGET_DB_KEYS:
        schema = get_schema(key)
        if schema.notion_database_id is None:
            raise RuntimeError(f"{key}: notion_database_id is not set")

        payload = build_update_properties_payload(schema)
        response = requests.patch(
            f"{NOTION_API_BASE}/databases/{schema.notion_database_id}",
            headers=headers,
            json=payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        print(f"updated: {schema.display_name} ({schema.notion_database_id})")


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

    update_target_databases(api_key)
    print("\n完了しました。")


if __name__ == "__main__":
    main()
