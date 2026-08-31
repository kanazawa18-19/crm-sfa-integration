"""「同期対象と宣言されているのに、対応表に無い項目」を固定するガード。

2026-08-31の棚卸しで、`sync_scope=ALL_TOOLS` と宣言されているのに変換表に無い項目が
大量に見つかった。宣言と実装が二重管理になっていて、片方だけでは誰も気づけない。

このテストは**現状のズレを理由付きで列挙して固定する**。新しくプロパティを足したとき、
変換表への登録を忘れると失敗する。既知のズレを解消したら、下の表から消すこと
（消し忘れても、対応済みになった項目が表に残っていれば失敗する）。

**値は理由。空文字は許さない**（obasan-qualityレビューWARN、2026-08-31: 理由なしで
1行足せるなら、この表は「とりあえず追加する儀式」になって形骸化する）。
"""

from __future__ import annotations

from src.db_schema.base import PropertyType, Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.outbound_field_mapping import (
    kintone_outbound_field_names,
    zoho_outbound_field_names,
)
from src.sync_engine.known_sync_gaps import KNOWN_UNMAPPED_FIELDS, KNOWN_UNMAPPED_RELATIONS
from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS
from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS

_INBOUND_TABLES = {Tool.ZOHO: ZOHO_LABEL_FIELD_MAPPINGS, Tool.KINTONE: KINTONE_FIELD_TRANSFORMS}
_OUTBOUND_TABLES = {
    Tool.ZOHO: zoho_outbound_field_names,
    Tool.KINTONE: kintone_outbound_field_names,
}


def _known_gaps() -> set[tuple[Tool, str, str]]:
    return set(KNOWN_UNMAPPED_RELATIONS) | set(KNOWN_UNMAPPED_FIELDS)


def _actual_inbound_gaps() -> set[tuple[Tool, str, str]]:
    gaps: set[tuple[Tool, str, str]] = set()
    for tool, table in _INBOUND_TABLES.items():
        for schema in ALL_SCHEMAS:
            if schema.key not in table:
                continue
            mapped = {property_name for (property_name, _transform) in table[schema.key].values()}
            for prop in schema.properties:
                if tool in prop.sync_scope.synced_tools and prop.name not in mapped:
                    gaps.add((tool, schema.key, prop.name))
    return gaps


def test_no_newly_declared_property_is_missing_from_the_transform_table() -> None:
    """宣言だけ足して対応表を忘れると、値が黙って捨てられる。ここで落とす。"""
    unexpected = _actual_inbound_gaps() - _known_gaps()

    assert not unexpected, (
        "同期対象と宣言されているのに変換表に無い項目があります。"
        "変換表へ登録するか、意図的なら既知の表へ理由付きで追加してください: "
        f"{sorted(unexpected)}"
    )


def test_known_gap_list_has_no_stale_entries() -> None:
    """対応済みになった項目が既知の表に残り続けないようにする。"""
    resolved = _known_gaps() - _actual_inbound_gaps()

    assert not resolved, f"対応済みなので既知の表から消してください: {sorted(resolved)}"


def test_every_known_gap_states_a_reason() -> None:
    """理由なしで既知の表へ足せないようにする。"""
    for key, reason in {**KNOWN_UNMAPPED_RELATIONS, **KNOWN_UNMAPPED_FIELDS}.items():
        assert reason.strip(), f"理由が空です: {key}"


def test_relation_gap_list_only_contains_relation_properties() -> None:
    """2つの表の取り違えを防ぐ（片方だけ見て判断されると誤解を生む）。"""
    types = {
        (schema.key, prop.name): prop.property_type
        for schema in ALL_SCHEMAS
        for prop in schema.properties
    }
    for _tool, db_key, property_name in KNOWN_UNMAPPED_RELATIONS:
        assert types[(db_key, property_name)] is PropertyType.RELATION
    for _tool, db_key, property_name in KNOWN_UNMAPPED_FIELDS:
        assert types[(db_key, property_name)] is not PropertyType.RELATION


def test_outbound_coverage_matches_the_passthrough_type_rule() -> None:
    """**Notion→外部**の欠落も固定する（obasan-qualityレビューWARN、2026-08-31）。

    外向きの対応表は「値をそのまま送れる型だけ」という規則で機械的に決まる。
    その規則から外れた欠落（＝規則では送れるはずなのに送り先が決まらない項目）だけを
    ここで洗い出し、既知のものに限定する。増えたら気づけるようにするのが目的。
    """
    from src.sync_engine.outbound_field_mapping import _PASSTHROUGH_TYPES

    # 送り先の候補が複数あり、ラベル完全一致で絞れないもの。ここに挙げた分だけを許す。
    # 送り先の候補が複数あり、ラベル完全一致でも実データでも絞れないもの。
    # 今はゼロ（「議事録・録画リンク」は実測でZohoの`Notta`に決着した、2026-08-31）。
    known_ambiguous: dict[tuple[Tool, str, str], str] = {}

    unexpected: set[tuple[Tool, str, str]] = set()
    for tool, build_table in _OUTBOUND_TABLES.items():
        table = build_table()
        inbound = _INBOUND_TABLES[tool]
        for schema in ALL_SCHEMAS:
            if schema.key not in inbound:
                continue
            inbound_properties = {
                property_name for (property_name, _transform) in inbound[schema.key].values()
            }
            for prop in schema.properties:
                if prop.property_type not in _PASSTHROUGH_TYPES:
                    continue  # 値の逆変換が要る型は規則どおりの対象外。
                if prop.name not in inbound_properties:
                    continue  # 入る方向にも無いので、既知の表（上）の管轄。
                if prop.name not in table.get(schema.key, {}):
                    unexpected.add((tool, schema.key, prop.name))

    assert not unexpected - set(known_ambiguous), (
        "値をそのまま送れる型なのに送り先が決まらない項目があります: "
        f"{sorted(unexpected - set(known_ambiguous))}"
    )
    assert not set(known_ambiguous) - unexpected, (
        "解消済みなので既知リストから消してください: "
        f"{sorted(set(known_ambiguous) - unexpected)}"
    )
