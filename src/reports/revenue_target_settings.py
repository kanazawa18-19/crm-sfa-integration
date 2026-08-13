"""事業計画スプレッドシートへの「ポインタ」を1件だけ保持するNotion裏付け設定ストア。

`src/reports/revenue_target_sheet.py`のモジュールdocstring・
`RevenueTargetSheetPointer`で説明している通り、目標値そのもの（金額・件数）はNotion側に
一切複製しない方針とした。一方でポインタ自体（どのスプレッドシート・どのシート名か）は
どこかに永続化する必要があり、Vercelの`/tmp`はコールドスタートのたびに消えるため
（`docs/id_mapping_persistence_note.md`）ローカルファイル保存は使えない。

`src/sync_engine/notion_id_mapping.py`の`NotionIdMappingStore`（「実データ6DBとは別の
専用Notion database + 専用APIトークンでレコードを永続化する」という、このコードベースで
既に確立された「軽量な設定の永続化」パターン）を踏襲する。ただし本ストアが保持するのは
常にちょうど1件（アプリ全体で1つの事業計画スプレッドシートしか設定しない）であり、
`NotionIdMappingStore`のような外部ID一意性チェック・多レコードのクエリ機能は不要なため、
`IdMappingStore`インターフェースは実装せず、専用の薄い実装として独立させている。

■ 対象Notion database
既存の「データマッピング」DB（`_DEFAULT_ID_MAPPING_DATABASE_ID`）を再利用せず、新規に
専用のNotion database（「事業計画連携設定」）を作る方針とした。ID マッピングDBは
「実データ同期のkintone_id/zoho_id」という明確なスコープを持つ既存ストアであり、そこへ
無関係な関心事（本ストアの3つの文字列フィールド）を間借りさせると、そのスコープが
曖昧になる（obasan-quality観点での可読性低下）ため避けた。新規DBの作成手順は
`scripts/setup_revenue_target_settings_db.py`・`docs/revenue_target_sheet_note.md`参照
（本番Notionワークスペースへの実際の作成は、この変更をレビューする側が判断の上、
別途実行すること）。

■ APIトークン
`SYNC_ID_MAPPING_NOTION_API_KEY`（IDマッピング専用の低ボリュームトークン）を既定で
再利用する。本ストアの読み書きは設定画面の保存操作・レポートバッチの目標値解決時のみで
高頻度アクセスではなく、`NotionIdMappingStore`と同様に実データ同期用`NOTION_API_KEY`の
レート制限枠を奪い合わないことが目的のため、専用トークンをさらに1つ増やすよりも
妥当と判断した。`REVENUE_TARGET_SETTINGS_NOTION_API_KEY`で明示的に上書きも可能。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.reports.revenue_target_sheet import RevenueTargetSheetPointer
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RATE_LIMIT_RETRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"

# 単一レコードを一意に特定するための固定タイトル値。
# database内に複数ページが誤って作られてしまった場合でも、この値のページのみを対象とする。
_SETTINGS_TITLE_KEY = "revenue_target_sheet_pointer"


class RevenueTargetSettingsStoreApiError(ApiError):
    """本ストアのNotion API呼び出し失敗時に送出する例外。"""


@dataclass(frozen=True)
class RevenueTargetSettingsRecord:
    """永続化された1件のポインタ設定（更新日時つき）。"""

    pointer: RevenueTargetSheetPointer
    updated_at: datetime


def _rich_text_value(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    parts = prop.get("rich_text") or []
    text = "".join(part.get("plain_text", "") for part in parts)
    return text or None


def _date_value(prop: dict[str, Any] | None) -> datetime | None:
    if not prop:
        return None
    date = prop.get("date")
    start = date.get("start") if date else None
    if not start:
        return None
    # NotionのISO日時は末尾"Z"を付けて返ることがあり、Python 3.10以前のdatetime.fromisoformat
    # は"Z"を解釈できない（NotionIdMappingStoreのparse_iso_datetimeと同じ注意点だが、循環import
    # を避けるためここでは素朴にreplaceする軽量版で十分）。
    return datetime.fromisoformat(start.replace("Z", "+00:00"))


def _rich_text_content(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    return [{"type": "text", "text": {"content": value}}]


def _page_to_record(page: dict[str, Any]) -> RevenueTargetSettingsRecord:
    properties = page.get("properties") or {}
    updated_at = _date_value(properties.get("updated_at"))
    if updated_at is None:
        raise RevenueTargetSettingsStoreApiError(
            500, "設定レコードにupdated_atが無い不正な状態です"
        )
    return RevenueTargetSettingsRecord(
        pointer=RevenueTargetSheetPointer(
            spreadsheet_id=_rich_text_value(properties.get("spreadsheet_id")) or "",
            mrr_sheet_name=_rich_text_value(properties.get("mrr_sheet_name")),
            unit_count_sheet_name=_rich_text_value(properties.get("unit_count_sheet_name")),
        ),
        updated_at=updated_at,
    )


def _record_to_properties(pointer: RevenueTargetSheetPointer, updated_at: datetime) -> dict[str, Any]:
    return {
        "key": {"title": [{"type": "text", "text": {"content": _SETTINGS_TITLE_KEY}}]},
        "spreadsheet_id": {"rich_text": _rich_text_content(pointer.spreadsheet_id)},
        "mrr_sheet_name": {"rich_text": _rich_text_content(pointer.mrr_sheet_name)},
        "unit_count_sheet_name": {"rich_text": _rich_text_content(pointer.unit_count_sheet_name)},
        "updated_at": {"date": {"start": updated_at.isoformat()}},
    }


class RevenueTargetSettingsStore:
    """事業計画スプレッドシートへのポインタを1件だけ保持するNotion裏付けストア
    （モジュールdocstring参照）。

    `database_id`省略時は`REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID`環境変数を使う
    （こちらは`NotionIdMappingStore`と異なりハードコードされた既定値を持たない。専用DBが
    まだ本番Notionワークスペースに作られていないため。`docs/revenue_target_sheet_note.md`
    参照）。未設定時は`ValueError`を送出する。
    `api_key`省略時は`REVENUE_TARGET_SETTINGS_NOTION_API_KEY`環境変数、それも未設定なら
    `SYNC_ID_MAPPING_NOTION_API_KEY`環境変数を使う（モジュールdocstring参照）。
    """

    def __init__(
        self,
        database_id: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        notion_version: str = _NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._database_id = (
            database_id
            if database_id is not None
            else os.environ.get("REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID")
        )
        if not self._database_id:
            raise ValueError(
                "REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID environment variable (or "
                "database_id argument) is required but not set"
            )
        self._api_key = (
            api_key
            if api_key is not None
            else os.environ.get("REVENUE_TARGET_SETTINGS_NOTION_API_KEY")
            or os.environ.get("SYNC_ID_MAPPING_NOTION_API_KEY")
        )
        if not self._api_key:
            raise ValueError(
                "REVENUE_TARGET_SETTINGS_NOTION_API_KEY or SYNC_ID_MAPPING_NOTION_API_KEY "
                "environment variable (or api_key argument) is required but not set"
            )
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rate_limit_retries = max_rate_limit_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self._notion_version,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, json_body: Any | None = None, idempotent: bool = True):
        return request_with_retry(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            json_body=json_body,
            timeout=self._timeout,
            max_retries=self._max_retries,
            max_rate_limit_retries=self._max_rate_limit_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def _find_page(self) -> dict[str, Any] | None:
        response = self._request(
            "POST",
            f"/databases/{self._database_id}/query",
            json_body={
                "filter": {"property": "key", "title": {"equals": _SETTINGS_TITLE_KEY}},
                "page_size": 1,
            },
        )
        raise_for_error(response, RevenueTargetSettingsStoreApiError)
        results = response.json().get("results") or []
        return results[0] if results else None

    def get(self) -> RevenueTargetSettingsRecord | None:
        """設定済みのポインタを返す。未設定（レコードが無い）ならNoneを返す。"""
        page = self._find_page()
        return _page_to_record(page) if page else None

    def upsert(
        self, pointer: RevenueTargetSheetPointer, *, updated_at: datetime | None = None
    ) -> RevenueTargetSettingsRecord:
        """ポインタを保存する（既存レコードがあれば更新、無ければ新規作成）。"""
        resolved_updated_at = updated_at or datetime.now()
        properties = _record_to_properties(pointer, resolved_updated_at)

        existing_page = self._find_page()
        if existing_page is None:
            body = {"parent": {"database_id": self._database_id}, "properties": properties}
            # 作成系（非冪等）操作のためリトライしない（NotionIdMappingStore.upsertと同じ方針）。
            response = self._request("POST", "/pages", json_body=body, idempotent=False)
        else:
            response = self._request(
                "PATCH", f"/pages/{existing_page['id']}", json_body={"properties": properties}
            )
        raise_for_error(response, RevenueTargetSettingsStoreApiError)

        return RevenueTargetSettingsRecord(pointer=pointer, updated_at=resolved_updated_at)


def build_revenue_target_settings_store() -> RevenueTargetSettingsStore | None:
    """設定ストアを構築する。必要な環境変数が未設定の場合はNoneを返す（例外にしない）。

    `RevenueTargetSettingsStore.__init__`は環境変数未設定時に`ValueError`を送出するが、
    呼び出し側（`src.reports.batch`のレポート生成・目標値解決フロー）にとってこれは
    「まだ事業計画スプレッドシート連携を設定していない」という正常な状態の1つであり、
    環境変数フォールバックへ処理を続けたい。そのためこの関数でValueErrorを吸収し、
    Noneという「未構成」を表す値に変換する。
    """
    try:
        return RevenueTargetSettingsStore()
    except ValueError:
        return None
