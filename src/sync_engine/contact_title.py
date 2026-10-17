"""連絡先の表示タイトルと、同期・照合に使う氏名を分離する。I/Oは持たない。"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

NAME = "名前"
CANONICAL_NAME = "氏名（連携用）"
MANAGED = "タイトル自動化"


def canonical_values(db_key: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """対象フラグのある連絡先だけ、純氏名を内部の「名前」として扱う。"""
    result = dict(values)
    if (db_key == "contact" and values.get(MANAGED) is not True
            and values.get(CANONICAL_NAME) and values.get(NAME) != values[CANONICAL_NAME]):
        raise ValueError("氏名保持済みの連絡先で自動化対象が解除されています")
    if db_key == "contact" and values.get(MANAGED) is True:
        if CANONICAL_NAME not in values:
            raise ValueError("自動タイトルの連絡先に氏名保持欄がありません")
        # 空にされた氏名を装飾済みタイトルで復活させない。
        result[NAME] = values.get(CANONICAL_NAME)
    return result


def canonical_properties(
    db_key: str,
    properties: Mapping[str, Any],
    changed_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """生のプロパティを変更せず、同期用の氏名だけ差し替える。

    changed_idsは、呼び出し元で今回の通知対象を確定した場合のみ渡す。
    表示タイトルだけの変更を純氏名の変更として配送しない。
    """
    result = dict(properties)
    marker = properties.get(MANAGED) or {}
    if db_key != "contact":
        return result
    if marker.get("checkbox") is not True:
        if changed_ids is not None and marker.get("id") in changed_ids:
            raise ValueError("自動化対象の解除通知は氏名として配送できません")
        def text_value(raw):
            return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                           for item in (raw or {}).get((raw or {}).get("type"), []))
        pure = text_value(properties.get(CANONICAL_NAME))
        if pure and pure != text_value(properties.get(NAME)):
            raise ValueError("氏名保持済みの連絡先で自動化対象が解除されています")
        return result
    raw_name = properties.get(CANONICAL_NAME)
    if not isinstance(raw_name, Mapping) or raw_name.get("type") != "rich_text":
        raise ValueError("自動タイトルの連絡先に氏名保持欄がありません")
    result.pop(NAME, None)
    # 初期化フラグだけの変更は、空の保持欄を「氏名の消去」と解釈しない。
    empty_initialization = (changed_ids is not None and marker.get("id") in changed_ids
                            and raw_name.get("id") not in changed_ids and not raw_name.get("rich_text"))
    if not empty_initialization and (changed_ids is None or raw_name.get("id") in changed_ids or marker.get("id") in changed_ids):
        result[NAME] = dict(raw_name)
    # 制御用の項目は通常の同期・照合へ渡さない。
    result.pop(CANONICAL_NAME, None)
    result.pop(MANAGED, None)
    return result


def name_filter(property_name: str, condition: Mapping[str, Any], *, enabled: bool) -> dict:
    """新旧が混在する期間も、元氏名による完全一致検索を維持する。"""
    original = {"property": property_name, "title": dict(condition)}
    if not enabled or property_name != NAME:
        return original
    return {"or": [original, {"property": CANONICAL_NAME, "rich_text": dict(condition)}]}
