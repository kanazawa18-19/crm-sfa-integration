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

#: 値をそのまま外部へ渡してよいNotionプロパティ型。**ここに無い型は全て対象外**。
#: 除外している型と理由（`PropertyType`の全列挙に対する差分、2026-08-31）:
#:   SELECT / STATUS / MULTI_SELECT / CHECKBOX … 取り込みが多対一なので機械的に逆変換できない
#:     （Zohoの「メルアポ」も「メール」もNotionでは「メール」になる。どちらへ戻すか決められない）
#:   RELATION / USER … 値がNotionのページID・ユーザーIDで、外部ツールには存在しない識別子
#:   DATETIME / JSON_TEXT … 外部側の受け入れ形式を実データで確認できていないため保留
#:   ROLLUP / FORMULA / UNIQUE_ID / CREATED_TIME / LAST_EDITED_TIME / CREATED_BY / FILES
#:     … Notion側が計算・自動採番する読み取り専用の型（`sync_scope=INTERNAL`が強制される）
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
def _zoho_api_names_by_label() -> dict[str, dict[str, list[str]]]:
    """{モジュール名: {ラベル: [api_name, ...]}}。

    同一ラベルが複数のapi_nameに割り当たることが実際にある（例: Deals の「作成日時」は
    `Created_Time`と`field42`の2つ）。**ここで先勝ちに畳み込むと曖昧さが消えてしまい、
    `_choose()`の安全弁が働かない**ので、候補は全部持ち上げる。
    """
    raw: dict[str, dict[str, str]] = json.loads(_ZOHO_FIELD_MAPPING_PATH.read_text(encoding="utf-8"))
    by_label: dict[str, dict[str, list[str]]] = {}
    for module, fields in raw.items():
        table: dict[str, list[str]] = {}
        for api_name, label in fields.items():
            table.setdefault(label, []).append(api_name)
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


def _choose_unique_outbound_target(
    property_name: str, candidates: list[tuple[str, str]]
) -> str | None:
    """候補が複数あるとき、**ラベルがNotionのプロパティ名と完全一致するもの**を採る。

    一致するものが無ければ、どちらへ書くべきか機械的には決められないのでNoneを返す
    （誤った項目を上書きするより、書かずに残す方が安全）。
    """
    if len(candidates) == 1:
        return candidates[0][1]
    exact = [external for label, external in candidates if label == property_name]
    if len(exact) == 1:
        return exact[0]
    # 完全一致が2つ以上あるのは「同じラベルが複数のフィールドに付いている」ケース。
    # どちらが本命か判断材料が無いので、ここも対象外に倒す。
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
    api_by_label = _zoho_api_names_by_label()
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
            for api_name in labels.get(label, ()):
                candidates.setdefault(property_name, []).append((label, api_name))
        table = {}
        for property_name, found in candidates.items():
            chosen = _choose_unique_outbound_target(property_name, found)
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
            chosen = _choose_unique_outbound_target(property_name, found)
            if chosen is not None:
                table[property_name] = chosen
        result[db_key] = table
    return result


@lru_cache(maxsize=1)
def _date_properties() -> dict[str, frozenset[str]]:
    """{db_key: DATE型のプロパティ名の集合}。"""
    return {
        schema.key: frozenset(
            prop.name for prop in schema.properties if prop.property_type is PropertyType.DATE
        )
        for schema in ALL_SCHEMAS
    }


def _normalize_outbound_value(db_key: str, property_name: str, value: Any) -> Any:
    """外部が受け取れる形へ最小限だけ整える。

    Notionの日付は時刻付き（`2026-08-31T09:00:00.000+09:00`）で返ることがあり、
    kintoneのDATEフィールドもZohoの日付項目も日付部分しか受け付けない。

    **DATE型のプロパティにだけ適用する。** 型を見ずに全文字列へ掛けると、
    たまたまISO日時から始まる自由記述テキストが日付だけに切り詰められ、本文が消える
    （shirokuma-secレビューWARN、2026-08-31）。
    """
    if property_name not in _date_properties().get(db_key, frozenset()):
        return value
    if isinstance(value, str):
        match = _ISO_DATETIME_RE.match(value)
        if match:
            return match.group(1)
    return value


def translate_properties(
    outbound_table: dict[str, dict[str, str]], db_key: str | None, properties: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Notionのプロパティ名を外部のフィールド名へ置き換える。

    戻り値は（置き換え済みのdict, 置き換えられなかったプロパティ名の一覧）。

    db_keyが分からない場合は**全項目を対象外にする**。ここで素通しすると、
    Notionのプロパティ名がそのまま外部APIへ渡るという元の不具合に戻るため
    （「変換できないから、とりあえずそのまま送る」は、送った気になるだけで何も起きない）。
    """
    table = outbound_table.get(db_key) if db_key is not None else None
    if table is None:
        return {}, list(properties)
    translated: dict[str, Any] = {}
    unmapped: list[str] = []
    for property_name, value in properties.items():
        external = table.get(property_name)
        if external is None:
            unmapped.append(property_name)
            continue
        if _is_empty(value):
            # **空値は送らない**（ChatGPTクロスレビューBLOCKER対応、2026-08-31）。
            # 項目名の対応が正しくても、Notion側が空のまま送れば外部の既存値が消える。
            # 「値の変更」と「値の削除」は別物として扱い、削除は今は伝播させない。
            # 消したいときにどう伝えるかが決まるまで、消さない側へ倒す。
            unmapped.append(property_name)
            continue
        translated[external] = _normalize_outbound_value(db_key, property_name, value)
    return translated, unmapped


def _is_empty(value: Any) -> bool:
    """外部の既存値を消しにいく値かどうか。0やFalseは「値がある」として扱う。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False
