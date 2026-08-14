"""Notion Query Database APIのfilterを使った汎用ページ検索ヘルパー。

`web_engagement_webhook.py`（連絡先DBをメールアドレスで検索）と`meeting_sync`
（連絡先DBをメールアドレスで検索、アクション履歴DBをGoogleカレンダーイベントidで
検索）で重複していたロジックをここへ集約する。
"""

from __future__ import annotations

from typing import Any, Protocol

from src.sync_engine.clients.notion_client import parse_notion_property_value


class NotionQueryClient(Protocol):
    """本モジュールが検索対象のNotionクライアントに要求する最小インターフェース。"""

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


def find_page_id_by_email(
    client: NotionQueryClient, property_name: str, email: str
) -> str | None:
    """`property_name`（email型プロパティ）が`email`と一致するページを1件返す（無ければNone）。

    Notion API側の`email`フィルタ（`query_all_pages(filter=...)`）で絞り込んだ上で、
    大文字小文字の扱いを仕様として保証できないため、クライアント側でも`.lower()`での
    再比較を行う（`src/migration/notion_dedupe.py`のクライアント側フィルタのパターンを踏襲）。
    """
    normalized = email.strip().lower()
    candidates = client.query_all_pages(
        filter={"property": property_name, "email": {"equals": email.strip()}}
    )
    for page in candidates:
        props = page.get("properties") or {}
        if property_name not in props:
            continue
        value = parse_notion_property_value(props[property_name])
        if isinstance(value, str) and value.strip().lower() == normalized:
            return page["id"]
    return None


def find_page_id_by_title(
    client: NotionQueryClient, property_name: str, value: str
) -> str | None:
    """`property_name`（title型プロパティ）が`value`と完全一致するページを1件返す
    （無ければNone）。`find_page_id_by_email`と同様、Notion API側のフィルタで絞り込んだ
    上でクライアント側でも再比較する。あいまい一致（表記ゆれ吸収、`notion_dedupe.py`の
    ような名寄せ）は意図的に行わない完全一致専用のヘルパー——呼び出し元がこれを
    どう使うか（例: 一致しない場合に新規作成するかしないか）はこの関数の関知するところ
    ではなく、各呼び出し元のモジュールに委ねる。
    """
    normalized = value.strip()
    candidates = client.query_all_pages(
        filter={"property": property_name, "title": {"equals": normalized}}
    )
    for page in candidates:
        props = page.get("properties") or {}
        if property_name not in props:
            continue
        parsed = parse_notion_property_value(props[property_name])
        if isinstance(parsed, str) and parsed.strip() == normalized:
            return page["id"]
    return None


def find_page_id_by_text_property(
    client: NotionQueryClient, property_name: str, value: str
) -> str | None:
    """`property_name`（rich_text型プロパティ）が`value`と完全一致するページを1件返す
    （無ければNone）。

    `meeting_sync`のGoogleカレンダーイベントid重複チェック用。Notion API側の
    `rich_text`フィルタ（`equals`）で絞り込んだ上で、`find_page_id_by_email`と同様に
    クライアント側でも再比較する。
    """
    candidates = client.query_all_pages(
        filter={"property": property_name, "rich_text": {"equals": value}}
    )
    for page in candidates:
        props = page.get("properties") or {}
        if property_name not in props:
            continue
        parsed = parse_notion_property_value(props[property_name])
        if isinstance(parsed, str) and parsed == value:
            return page["id"]
    return None
