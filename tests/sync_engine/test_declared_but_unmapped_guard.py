"""「同期対象と宣言されているのに、対応表に無い項目」を固定するガード。

2026-08-31の棚卸しで、`sync_scope=ALL_TOOLS` と宣言されているのに変換表に無い項目が
大量に見つかった。宣言と実装が二重管理になっていて、片方だけでは誰も気づけない。

このテストは**現状のズレを明示的に列挙して固定する**。新しくプロパティを足したとき、
変換表への登録を忘れると失敗する。既知のズレを解消したら、下のリストから消すこと
（消し忘れても、逆にリストに残っている項目が対応済みになった時点で失敗する）。
"""

from __future__ import annotations

from src.db_schema.base import PropertyType, Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS
from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS

#: リレーション型の未対応。値がNotionのページIDなので、外部から取り込むには
#: 名前から相手のページを引き当てる「名寄せ」が要る（`src/relation_sync/`）。
#: 現状これがあるのは取引先マスターへの紐付けだけ。
KNOWN_UNMAPPED_RELATIONS: frozenset[tuple[Tool, str, str]] = frozenset(
    (
    (Tool.KINTONE, 'action', '案件名'),
    (Tool.KINTONE, 'action', '連絡先'),
    (Tool.KINTONE, 'action', '👯\u200d♀️ チェーンリスト'),
    (Tool.KINTONE, 'client_master', '【営業部】案件管理DB'),
    (Tool.KINTONE, 'client_master', '【営業部・パーソネル】アクション履歴DB'),
    (Tool.KINTONE, 'client_master', 'サービス・商品'),
    (Tool.KINTONE, 'client_master', 'チェーン'),
    (Tool.KINTONE, 'project', 'アクション履歴'),
    (Tool.KINTONE, 'project', 'サービス・商品'),
    (Tool.KINTONE, 'project', 'チェーン'),
    (Tool.KINTONE, 'project', '連絡先'),
    (Tool.ZOHO, 'action', '案件名'),
    (Tool.ZOHO, 'action', '連絡先'),
    (Tool.ZOHO, 'action', '👯\u200d♀️ チェーンリスト'),
    (Tool.ZOHO, 'chain', 'アクション履歴'),
    (Tool.ZOHO, 'chain', 'サービス・商品'),
    (Tool.ZOHO, 'chain', '案件管理'),
    (Tool.ZOHO, 'chain', '連絡先'),
    (Tool.ZOHO, 'chain', '👨\u200d👩\u200d👧\u200d👦 取引先マスター'),
    (Tool.ZOHO, 'client_master', '【営業部】案件管理DB'),
    (Tool.ZOHO, 'client_master', '【営業部・パーソネル】アクション履歴DB'),
    (Tool.ZOHO, 'client_master', 'サービス・商品'),
    (Tool.ZOHO, 'client_master', 'チェーン'),
    (Tool.ZOHO, 'contact', 'アクション履歴'),
    (Tool.ZOHO, 'contact', 'チェーン'),
    (Tool.ZOHO, 'contact', '案件管理'),
    (Tool.ZOHO, 'product', 'チェーン'),
    (Tool.ZOHO, 'product', '取引先マスター'),
    (Tool.ZOHO, 'product', '案件管理'),
    (Tool.ZOHO, 'project', 'アクション履歴'),
    (Tool.ZOHO, 'project', 'サービス・商品'),
    (Tool.ZOHO, 'project', 'チェーン'),
    (Tool.ZOHO, 'project', '取引先マスター'),
    (Tool.ZOHO, 'project', '連絡先'),
    )
)

#: リレーション以外の未対応。単に変換表へ登録されていないだけで、
#: 登録すれば動くもの。実務で必要になった順に潰す。
KNOWN_UNMAPPED_FIELDS: frozenset[tuple[Tool, str, str]] = frozenset(
    (
    (Tool.KINTONE, 'action', 'アクション日'),
    (Tool.KINTONE, 'action', '先方担当者'),
    (Tool.KINTONE, 'action', '商談回数・電話回数・メール回数（何回目）'),
    (Tool.KINTONE, 'action', '導入フローとスケジュール'),
    (Tool.KINTONE, 'action', '議事録・録画リンク'),
    (Tool.KINTONE, 'client_master', '予算組の時期'),
    (Tool.KINTONE, 'client_master', '備考'),
    (Tool.KINTONE, 'client_master', '日付'),
    (Tool.KINTONE, 'client_master', '決算'),
    (Tool.KINTONE, 'project', '【例外】粗利'),
    (Tool.KINTONE, 'project', 'かつやさん'),
    (Tool.KINTONE, 'project', 'サイトコントローラー'),
    (Tool.KINTONE, 'project', 'サービス数（施設数）'),
    (Tool.KINTONE, 'project', 'ショット'),
    (Tool.KINTONE, 'project', 'テキスト'),
    (Tool.KINTONE, 'project', 'ネックポイント'),
    (Tool.KINTONE, 'project', 'ファーストタッチ'),
    (Tool.KINTONE, 'project', 'メモ'),
    (Tool.KINTONE, 'project', 'メールアドレス'),
    (Tool.KINTONE, 'project', '例外スイッチ（途中解約・複数サービス提案など）'),
    (Tool.KINTONE, 'project', '再アプローチ日'),
    (Tool.KINTONE, 'project', '問合せ'),
    (Tool.KINTONE, 'project', '失注日'),
    (Tool.KINTONE, 'project', '失注理由'),
    (Tool.KINTONE, 'project', '担当メンバー'),
    (Tool.KINTONE, 'project', '担当者名'),
    (Tool.KINTONE, 'project', '提案サービス'),
    (Tool.KINTONE, 'project', '案件名'),
    (Tool.KINTONE, 'project', '次回アクション'),
    (Tool.KINTONE, 'project', '次回アクション日'),
    (Tool.KINTONE, 'project', '決裁者名'),
    (Tool.KINTONE, 'project', '確度'),
    (Tool.KINTONE, 'project', '電話番号'),
    (Tool.ZOHO, 'action', '導入フローとスケジュール'),
    (Tool.ZOHO, 'chain', 'その他'),
    (Tool.ZOHO, 'chain', 'その他ブランド'),
    (Tool.ZOHO, 'chain', 'オルト'),
    (Tool.ZOHO, 'chain', 'ホテラボ'),
    (Tool.ZOHO, 'chain', 'メイリー'),
    (Tool.ZOHO, 'chain', 'リピッテ'),
    (Tool.ZOHO, 'chain', '三密'),
    (Tool.ZOHO, 'chain', '担当'),
    (Tool.ZOHO, 'client_master', '予算組の時期'),
    (Tool.ZOHO, 'client_master', '備考'),
    (Tool.ZOHO, 'client_master', '日付'),
    (Tool.ZOHO, 'client_master', '決算'),
    (Tool.ZOHO, 'contact', '担当メンバー'),
    (Tool.ZOHO, 'project', '【例外】粗利'),
    (Tool.ZOHO, 'project', 'ショット'),
    (Tool.ZOHO, 'project', '例外スイッチ（途中解約・複数サービス提案など）'),
    (Tool.ZOHO, 'project', '再アプローチ日'),
    (Tool.ZOHO, 'project', '担当メンバー'),
    (Tool.ZOHO, 'project', '提案サービス'),
    (Tool.ZOHO, 'project', '次回アクション日'),
    (Tool.ZOHO, 'project', '確度'),
    )
)

_TABLES = {Tool.ZOHO: ZOHO_LABEL_FIELD_MAPPINGS, Tool.KINTONE: KINTONE_FIELD_TRANSFORMS}


def _actual_gaps() -> set[tuple[Tool, str, str]]:
    gaps: set[tuple[Tool, str, str]] = set()
    for tool, table in _TABLES.items():
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
    known = KNOWN_UNMAPPED_RELATIONS | KNOWN_UNMAPPED_FIELDS
    unexpected = _actual_gaps() - known

    assert not unexpected, (
        "同期対象と宣言されているのに変換表に無い項目があります。"
        "変換表へ登録するか、意図的なら既知リストへ理由付きで追加してください: "
        f"{sorted(unexpected)}"
    )


def test_known_gap_list_has_no_stale_entries() -> None:
    """対応済みになった項目が既知リストに残り続けないようにする。"""
    known = KNOWN_UNMAPPED_RELATIONS | KNOWN_UNMAPPED_FIELDS
    resolved = known - _actual_gaps()

    assert not resolved, f"対応済みなので既知リストから消してください: {sorted(resolved)}"


def test_relation_gap_list_only_contains_relation_properties() -> None:
    """2つのリストの取り違えを防ぐ（片方だけ見て判断されると誤解を生む）。"""
    types = {
        (schema.key, prop.name): prop.property_type
        for schema in ALL_SCHEMAS
        for prop in schema.properties
    }
    for _tool, db_key, property_name in KNOWN_UNMAPPED_RELATIONS:
        assert types[(db_key, property_name)] is PropertyType.RELATION
    for _tool, db_key, property_name in KNOWN_UNMAPPED_FIELDS:
        assert types[(db_key, property_name)] is not PropertyType.RELATION
