"""Notion→kintone/Zoho の「項目名」逆変換。

2026-08-31の棚卸しで判明した問題への対応。

外部→Notionには2段階の対応表（`config/zoho_field_mapping.json` の api_name→ラベル、
`ZOHO_LABEL_FIELD_MAPPINGS` のラベル→Notionプロパティ）があるのに、
**Notion→外部には逆変換が存在しなかった**。そのため
`ZohoSyncTarget.upsert_record()` / `KintoneSyncTarget.upsert_record()` には
Notionのプロパティ名（例:「アクション種別」）がそのまま渡り、外部APIへ
そのまま送られていた。実測ではZohoのapi_nameと名前が一致するのは104項目中1項目、
kintoneのフィールドコードと一致するのは59項目中6項目しかない。

ここでは**既存の対応表を反転して**逆変換を作る。新しい対応表を手で書き起こすと
二重管理になり、片方だけ更新されて静かに壊れるため。

## 何を書き込み対象にするか（安全側の線引き）

反転できるのは「項目名」だけで、「値」の逆変換は別問題。
入力側の変換（例: Zohoの「メルアポ」→Notionの「メール」）は多対一なので、
機械的には反転できない。誤った選択肢を外部の選択肢フィールドへ書き込むと
APIエラーか、最悪サイレントに別の値が入る。

そこで v1 では**値の変換が要らない型だけ**を書き込み対象にする
（`_PASSTHROUGH_TYPES`）。選択肢・ステータス・複数選択・チェックボックス・
リレーションは対象外とし、`unmapped` として呼び出し元へ返す。
対象外の項目は「書き込めなかった」として扱われ、成功に数えない。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.db_schema.base import PropertyType
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS
from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS

logger = logging.getLogger(__name__)

_ZOHO_FIELD_MAPPING_PATH = Path(__file__).resolve().parents[2] / "config" / "zoho_field_mapping.json"

#: 値をそのまま外部へ渡してよいNotionプロパティ型。
#: 選択肢系（SELECT/STATUS/MULTI_SELECT）とCHECKBOX、RELATIONは値の逆変換が要るため除く。
#: RELATIONの値はNotionのページIDで、外部ツールには存在しない識別子なので原理的に送れない。
_PASSTHROUGH_TYPES = frozenset(
    {
        PropertyType.TITLE,
        PropertyType.TEXT,
        PropertyType.NUMBER,
        PropertyType.CURRENCY,
        PropertyType.DATE,
        PropertyType.EMAIL,
        PropertyType.PHONE,
        PropertyType.URL,
    }
)

_ISO_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")


@lru_cache(maxsize=1)
def _zoho_api_name_by_label() -> dict[str, dict[str, str]]:
    """{モジュール名: {ラベル: api_name}}。同一ラベルが複数api_nameに割り当たることがある。"""
    raw: dict[str, dict[str, str]] = json.loads(_ZOHO_FIELD_MAPPING_PATH.read_text(encoding="utf-8"))
    by_label: dict[str, dict[str, str]] = {}
    for module, fields in raw.items():
        table: dict[str, str] = {}
        for api_name, label in fields.items():
            # 先勝ち。ラベル重複時はどちらでも曖昧なので、下の候補選定で弾く。
            table.setdefault(label, api_name)
        by_label[module] = table
    return by_label


def _passthrough_properties() -> dict[str, dict[str, PropertyType]]:
    """{db_key: {プロパティ名: 型}}。値変換が不要な型のみ。"""
    result: dict[str, dict[str, PropertyType]] = {}
    for schema in ALL_SCHEMAS:
        result[schema.key] = {
            prop.name: prop.property_type
            for prop in schema.properties
            if prop.property_type in _PASSTHROUGH_TYPES
        }
    return result


def _choose(property_name: str, candidates: list[tuple[str, str]]) -> str | None:
    """候補が複数あるとき、**ラベルがNotionのプロパティ名と完全一致するもの**を採る。

    一致するものが無ければ、どちらへ書くべきか機械的には決められないのでNoneを返す
    （誤った項目を上書きするより、書かずに残す方が安全）。
    """
    if len(candidates) == 1:
        return candidates[0][1]
    exact = [external for label, external in candidates if label == property_name]
    if len(exact) == 1:
        return exact[0]
    logger.warning(
        "outbound_field_mapping: 送り先を一意に決められないため対象外にします "
        "(property=%r, candidates=%r)",
        property_name,
        [external for _label, external in candidates],
    )
    return None


@lru_cache(maxsize=1)
def zoho_outbound_field_names() -> dict[str, dict[str, str]]:
    """{db_key: {Notionプロパティ名: Zohoのapi_name}}。"""
    safe = _passthrough_properties()
    api_by_label = _zoho_api_name_by_label()
    result: dict[str, dict[str, str]] = {}
    for schema in ALL_SCHEMAS:
        allowed = safe.get(schema.key, {})
        labels = api_by_label.get(schema.zoho_api_module, {})
        candidates: dict[str, list[tuple[str, str]]] = {}
        for label, (property_name, _transform) in ZOHO_LABEL_FIELD_MAPPINGS.get(
            schema.key, {}
        ).items():
            if property_name not in allowed:
                continue
            api_name = labels.get(label)
            if api_name is None:
                continue
            candidates.setdefault(property_name, []).append((label, api_name))
        table = {}
        for property_name, found in candidates.items():
            chosen = _choose(property_name, found)
            if chosen is not None:
                table[property_name] = chosen
        result[schema.key] = table
    return result


@lru_cache(maxsize=1)
def kintone_outbound_field_names() -> dict[str, dict[str, str]]:
    """{db_key: {Notionプロパティ名: kintoneのフィールドコード}}。"""
    safe = _passthrough_properties()
    result: dict[str, dict[str, str]] = {}
    for db_key, transforms in KINTONE_FIELD_TRANSFORMS.items():
        allowed = safe.get(db_key, {})
        candidates: dict[str, list[tuple[str, str]]] = {}
        for field_code, (property_name, _transform) in transforms.items():
            if property_name not in allowed:
                continue
            candidates.setdefault(property_name, []).append((field_code, field_code))
        table = {}
        for property_name, found in candidates.items():
            chosen = _choose(property_name, found)
            if chosen is not None:
                table[property_name] = chosen
        result[db_key] = table
    return result


def _normalize_outbound_value(value: Any) -> Any:
    """外部が受け取れる形へ最小限だけ整える。

    Notionの日付は時刻付き（`2026-08-31T09:00:00.000+09:00`）で返ることがあり、
    kintoneのDATEフィールドもZohoの日付項目も日付部分しか受け付けない。
    """
    if isinstance(value, str):
        match = _ISO_DATETIME_RE.match(value)
        if match:
            return match.group(1)
    return value


def translate_properties(
    tool_table: dict[str, dict[str, str]], db_key: str | None, properties: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Notionのプロパティ名を外部のフィールド名へ置き換える。

    戻り値は（置き換え済みのdict, 置き換えられなかったプロパティ名の一覧）。

    db_keyが分からない場合は**全項目を対象外にする**。ここで素通しすると、
    Notionのプロパティ名がそのまま外部APIへ渡るという元の不具合に戻るため
    （「変換できないから、とりあえずそのまま送る」は、送った気になるだけで何も起きない）。
    """
    table = tool_table.get(db_key) if db_key is not None else None
    if table is None:
        return {}, list(properties)
    translated: dict[str, Any] = {}
    unmapped: list[str] = []
    for property_name, value in properties.items():
        external = table.get(property_name)
        if external is None:
            unmapped.append(property_name)
            continue
        translated[external] = _normalize_outbound_value(value)
    return translated, unmapped
