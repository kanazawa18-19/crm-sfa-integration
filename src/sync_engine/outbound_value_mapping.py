"""Notion→Zoho の「選択肢の値」の読み替え（2026-08-31）。

項目名の逆変換（`outbound_field_mapping.py`）だけでは、選択肢・ステータス・複数選択は
送れない。Notionの「失注」がZohoのどの選択肢なのかが決まらないため、これらの型は
まるごと対象外にしていた。ここでその対応を作る。

■ どうやって作るか（**手で書かない**）

対応表を手で書き起こすと、Zoho側で選択肢が増えたときに片方だけ古くなって静かに壊れる。
そこで、**既にある「Zoho→Notion」の変換を、Zohoの実際の選択肢一覧に対して流し、
その結果を反転する**。

    Zohoの選択肢「ランニング」 → （既存の取り込み変換）→ Notionの「月額ストック」
                                                    ↓ 反転
    Notionの「月額ストック」   →  Zohoの「ランニング」

こうすると、取り込み側の変換を直せば書き込み側も自動で追随する。二重管理にならない。

■ 決められないものは送らない

- 2つ以上のZoho選択肢が同じNotionの値になる場合（例: 「メルアポ」も「メール」も
  Notionでは「メール」）、どちらへ戻すべきか決められない。**名前が完全一致するものが
  あればそれを採り、無ければ対象外にする**（項目名の逆変換と同じ規則）。
- Notion側にしかない値は送らない。Zohoに無い選択肢を送れば弾かれるだけ。

対象外になったものは`unmapped_notion_values()`で一覧できる。実務で必要になったものから
`_DECIDED_VALUES`へ足していく（**足すときは理由も書くこと**）。

■ kintoneは対象外

現時点でNotion→kintoneに対応がある3項目（初期費用・月額費用・契約日）は全て
数値と日付で、選択肢が無い（2026-08-31時点）。必要になったら同じ作りを足す。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.db_schema.base import PropertyType
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.webhook_handlers.zoho_field_transforms import (
    SKIP_FIELD,
    ZOHO_LABEL_FIELD_MAPPINGS,
)

logger = logging.getLogger(__name__)

_PICKLIST_PATH = Path(__file__).resolve().parents[2] / "config" / "zoho_picklists.json"
_FIELD_MAPPING_PATH = Path(__file__).resolve().parents[2] / "config" / "zoho_field_mapping.json"

#: 値が入っていないことを表すZoho側の表記。読み替えの対象にしない。
_EMPTY_ZOHO_VALUES = frozenset({"-None-", "option1", "option2"})

#: 選択肢を持つNotionプロパティの型。
CHOICE_TYPES = frozenset(
    {PropertyType.SELECT, PropertyType.STATUS, PropertyType.MULTI_SELECT}
)

#: 機械的には決められないが、業務として決めた読み替え。
#: {(db_key, プロパティ名, Notionの値): Zohoの値}
#: **足すときは必ず理由をコメントで残すこと。** 今は空（決めたものが無い）。
_DECIDED_VALUES: dict[tuple[str, str, str], str] = {}


@lru_cache(maxsize=1)
def _zoho_picklists() -> dict[str, dict[str, Any]]:
    if not _PICKLIST_PATH.exists():
        logger.warning(
            "config/zoho_picklists.json がありません。選択肢の読み替えは行いません"
            "（scripts/fetch_zoho_picklists.py で作成できます）"
        )
        return {}
    return json.loads(_PICKLIST_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _api_names_by_label() -> dict[str, dict[str, list[str]]]:
    raw: dict[str, dict[str, str]] = json.loads(_FIELD_MAPPING_PATH.read_text(encoding="utf-8"))
    by_label: dict[str, dict[str, list[str]]] = {}
    for module, fields in raw.items():
        table: dict[str, list[str]] = {}
        for api_name, label in fields.items():
            table.setdefault(label, []).append(api_name)
        by_label[module] = table
    return by_label


def _apply_inbound(transform: Any, zoho_value: str) -> str | None:
    """取り込み側の変換を1つの選択肢に適用する。変換できなければNone。"""
    try:
        result = transform(zoho_value)
    except Exception:  # noqa: BLE001 (変換は呼び出し元の実装依存)
        return None
    if result is SKIP_FIELD or result is None:
        return None
    return str(result)


@lru_cache(maxsize=1)
def zoho_outbound_value_maps() -> dict[str, dict[str, dict[str, str]]]:
    """{db_key: {Notionプロパティ名: {Notionの値: Zohoの値}}}。"""
    picklists = _zoho_picklists()
    labels = _api_names_by_label()
    result: dict[str, dict[str, dict[str, str]]] = {}

    for schema in ALL_SCHEMAS:
        module_picks = picklists.get(schema.zoho_api_module, {})
        module_labels = labels.get(schema.zoho_api_module, {})
        per_property: dict[str, dict[str, str]] = {}

        for label, (property_name, transform) in ZOHO_LABEL_FIELD_MAPPINGS.get(
            schema.key, {}
        ).items():
            prop = next((p for p in schema.properties if p.name == property_name), None)
            if prop is None or prop.property_type not in CHOICE_TYPES:
                continue
            api_names = [a for a in module_labels.get(label, []) if a in module_picks]
            if len(api_names) != 1:
                continue  # 送り先の項目が一意に決まらない。項目名の逆変換と同じ規則。
            options = [
                option
                for option in module_picks[api_names[0]]["options"]
                if option not in _EMPTY_ZOHO_VALUES
            ]
            allowed = set(prop.options)

            # Zohoの実際の選択肢を取り込み変換に通し、その結果を反転する。
            candidates: dict[str, list[str]] = {}
            for option in options:
                notion_value = _apply_inbound(transform, option)
                if notion_value is None or notion_value not in allowed:
                    continue
                candidates.setdefault(notion_value, []).append(option)

            table: dict[str, str] = {}
            for notion_value, matched in candidates.items():
                decided = _DECIDED_VALUES.get((schema.key, property_name, notion_value))
                if decided is not None and decided in options:
                    table[notion_value] = decided
                elif len(matched) == 1:
                    table[notion_value] = matched[0]
                elif notion_value in matched:
                    # 完全一致するものがあればそれを採る（項目名の逆変換と同じ規則）。
                    table[notion_value] = notion_value
                else:
                    logger.info(
                        "outbound_value_mapping: 戻し先を一意に決められないため対象外にします "
                        "(db_key=%r, property=%r, notion_value=%r, candidates=%r)",
                        schema.key,
                        property_name,
                        notion_value,
                        matched,
                    )
            if table:
                per_property[property_name] = table
        result[schema.key] = per_property
    return result


def translate_choice_value(db_key: str, property_name: str, value: Any) -> Any | None:
    """Notionの選択肢の値をZohoの値へ読み替える。決められなければNone。

    複数選択は各要素を読み替える。**1つでも読み替えられない要素があればNoneを返す**
    （読み替えられた分だけ送ると、Notionでは付いている選択肢がZohoから消えるため）。
    """
    table = zoho_outbound_value_maps().get(db_key, {}).get(property_name)
    if not table:
        return None
    if isinstance(value, (list, tuple)):
        translated = [table.get(str(item)) for item in value]
        if any(item is None for item in translated):
            return None
        return translated
    return table.get(str(value))


def unmapped_notion_values() -> dict[tuple[str, str], list[str]]:
    """読み替え先が決まっていないNotionの選択肢の一覧（運用の確認用）。"""
    maps = zoho_outbound_value_maps()
    result: dict[tuple[str, str], list[str]] = {}
    for schema in ALL_SCHEMAS:
        for property_name, table in maps.get(schema.key, {}).items():
            prop = next(p for p in schema.properties if p.name == property_name)
            missing = [option for option in prop.options if option not in table]
            if missing:
                result[(schema.key, property_name)] = missing
    return result
