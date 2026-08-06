"""ダッシュボード表示専用の寛容なNotionプロパティパーサー。

`src.sync_engine.webhook_handlers.notion_webhook.parse_notion_property_value`は
書き込み同期用のパーサーであり、対応外の型（rollup/formula/unique_id/created_time等）は
`ValueError`を送出する設計（意図的、変更しない）。ダッシュボードは表示専用であり、
1プロパティの欠損・未対応型でページ全体の表示を落とさない方が望ましいため、本モジュールは
`parse_notion_property_value`がカバーする型はそのロジックを再利用しつつ、追加でrollup/
formula/unique_id/created_time/last_edited_time/created_by/filesを扱い、未知の型は
例外を投げずNoneを返す寛容な実装とする。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.db_schema.base import DatabaseSchema
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value

logger = logging.getLogger(__name__)

# parse_notion_property_valueが対応しているNotionプロパティ型（そのまま委譲する）。
# "people"はここに含めない（下記の理由によりダッシュボード専用の別処理を行う）。
_DELEGATED_TYPES = frozenset(
    {
        "title",
        "rich_text",
        "select",
        "status",
        "multi_select",
        "number",
        "checkbox",
        "date",
        "email",
        "phone_number",
        "url",
        "relation",
    }
)


def _parse_people(prop: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`people`型プロパティを`[{"id":..., "name":...}, ...]`へ変換する。

    `parse_notion_property_value`（書き込み同期用）はIDのみを返すが、Notion APIの
    ページプロパティレスポンスには各ユーザーの`name`が直接埋め込まれている
    （インテグレーションに「ユーザー情報の読み取り」権限がある場合）。実データ確認の結果、
    `GET /v1/users`（ワークスペースメンバー一覧）にはゲストユーザー等の理由で現れない
    ユーザーが存在することが判明したため、`NotionUserDirectory`によるID→名前の別解決に
    頼らず、ページ自体に埋め込まれた`name`を最優先で使う（`GET /v1/users`は`name`が
    欠落しているケース向けのフォールバックとしてのみ利用する）。
    """
    return [
        {"id": person.get("id"), "name": person.get("name")}
        for person in prop.get("people") or []
    ]


def parse_notion_property_for_display(prop: Mapping[str, Any]) -> Any:
    """Notion APIのプロパティ値オブジェクトを表示用の素のPython値へ変換する。

    未知の型は例外を投げずNoneを返す（表示専用のため1プロパティの欠損でページ全体を
    落とさない設計）。
    """
    prop_type = prop.get("type")

    if prop_type in _DELEGATED_TYPES:
        return parse_notion_property_value(prop)

    if prop_type == "people":
        return _parse_people(prop)

    if prop_type == "rollup":
        rollup = prop.get("rollup") or {}
        rollup_type = rollup.get("type")
        if rollup_type == "array":
            return [parse_notion_property_for_display(item) for item in rollup.get("array") or []]
        if rollup_type == "number":
            return rollup.get("number")
        if rollup_type == "date":
            return rollup.get("date")
        return None

    if prop_type == "formula":
        formula = prop.get("formula") or {}
        formula_type = formula.get("type")
        if formula_type in ("string", "number", "boolean", "date"):
            return formula.get(formula_type)
        return None

    if prop_type == "unique_id":
        unique_id = prop.get("unique_id") or {}
        prefix = unique_id.get("prefix") or ""
        number = unique_id.get("number")
        return f"{prefix}{number}"

    if prop_type in ("created_time", "last_edited_time"):
        return prop.get(prop_type)

    if prop_type == "created_by":
        created_by = prop.get("created_by") or {}
        return {"id": created_by.get("id"), "name": None}

    if prop_type == "files":
        return [file.get("name") for file in prop.get("files") or []]

    return None


def page_to_display_dict(
    page: Mapping[str, Any], schema: DatabaseSchema
) -> tuple[dict[str, Any], set[str]]:
    """Notionページの`properties`を、スキーマで検証しつつ表示用のフラット辞書へ変換する。

    未定義プロパティ（スキーマに存在しない列）はスキップする。大量ページ処理時に
    レコード単位で警告ログを出すとログが肥大化するため、ここでは`logger.debug`のみ出力し、
    スキップしたプロパティ名の集合を2つ目の戻り値として返す（呼び出し元で全ページ分を
    集約し、ユニーク集合として1回だけ`logger.warning`する想定。
    `src/api/dashboard_service.py`の`NotionDataSource.get_projects()`/`get_actions()`参照）。
    1つ目の戻り値の`notion_page_id`キーで`page["id"]`も含める。
    """
    result: dict[str, Any] = {"notion_page_id": page["id"]}
    skipped_properties: set[str] = set()
    for name, value in (page.get("properties") or {}).items():
        try:
            schema.get_property(name)
        except KeyError:
            logger.debug(
                "ignoring unknown Notion property '%s' for db_key=%r (not in schema)",
                name,
                schema.key,
            )
            skipped_properties.add(name)
            continue
        result[name] = parse_notion_property_for_display(value)
    return result, skipped_properties
