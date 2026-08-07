"""案件管理DB（Notion）から、ドキュメント生成に必要な項目のみを取り出すヘルパー。

見積書・申込書・契約書の3生成器が共通して必要とする項目（案件名・提案サービス・取引先名・
メモ・担当者名）をまとめて取得する。担当メンバーの表示名解決は`src/api/notion_display.py`の
`_parse_people`と同じ方針（ページに埋め込まれた`name`をそのまま使う）を、既存実装
（`page_to_display_dict`）を再利用することで踏襲する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.api.notion_display import page_to_display_dict
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA
from src.sync_engine.clients.notion_client import HttpNotionClient

PROP_案件名 = "案件名"
PROP_提案サービス = "提案サービス"
PROP_取引先マスター = "取引先マスター"
PROP_メモ = "メモ"
PROP_担当メンバー = "担当メンバー"
PROP_取引先名 = "取引先名"


@dataclass(frozen=True)
class ProjectDocumentData:
    project_name: str
    proposed_services: list[str]
    client_name: str | None
    memo: str | None
    assignee_name: str | None


def _first_person_name(people: Any) -> str | None:
    """`page_to_display_dict`が返す`担当メンバー`（`[{"id":..., "name":...}, ...]`）の
    先頭1人の表示名を返す。"""
    if not isinstance(people, list) or not people:
        return None
    first = people[0]
    if not isinstance(first, dict):
        return None
    name = first.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def fetch_project_document_data(
    notion_page_id: str,
    *,
    notion_client: Any | None = None,
    client_master_client: Any | None = None,
) -> ProjectDocumentData:
    """案件管理DBの1ページを取得し、ドキュメント生成に必要な項目を組み立てる。"""
    project_client = notion_client or HttpNotionClient(
        PROJECT_SCHEMA.key, PROJECT_SCHEMA.notion_database_id
    )
    raw_page = project_client.get_raw_page(notion_page_id)
    record, _skipped = page_to_display_dict(raw_page, PROJECT_SCHEMA)

    client_ids = record.get(PROP_取引先マスター) or []
    client_name: str | None = None
    if client_ids:
        cm_client = client_master_client or HttpNotionClient(
            CLIENT_MASTER_SCHEMA.key, CLIENT_MASTER_SCHEMA.notion_database_id
        )
        client_page = cm_client.get_page(str(client_ids[0]))
        if client_page is not None:
            client_name = client_page.get(PROP_取引先名)

    return ProjectDocumentData(
        project_name=record.get(PROP_案件名) or "",
        proposed_services=list(record.get(PROP_提案サービス) or []),
        client_name=client_name,
        memo=record.get(PROP_メモ),
        assignee_name=_first_person_name(record.get(PROP_担当メンバー)),
    )
