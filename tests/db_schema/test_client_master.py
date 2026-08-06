from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.base import PropertyType


def test_client_master_schema_notion_database_id_matches_real_data() -> None:
    assert CLIENT_MASTER_SCHEMA.notion_database_id == "b8c17123-96a1-429e-82f1-9d39595c9861"


def test_client_master_schema_kojushu_property_options_match_real_data() -> None:
    prop = CLIENT_MASTER_SCHEMA.get_property("顧客種別")
    assert set(prop.options) == {
        "宿泊施設",
        "決済業社",
        "ホテル運営コンサル",
        "パートナー",
        "クリニック・医院",
        "【競合】ホテルWEB支援",
        "システムベンダー",
        "WEBマーケティング会社",
    }


def test_client_master_schema_prefecture_property_options_match_real_data() -> None:
    prop = CLIENT_MASTER_SCHEMA.get_property("都道府県")
    assert set(prop.options) == {
        "和歌山県", "三重県", "静岡県", "愛知県", "群馬", "岐阜県", "北海道", "大阪府",
        "滋賀県", "埼玉県", "茨城県", "東京都", "島根県", "広島県", "茨城", "青森",
        "東京", "兵庫県", "高知県", "沖縄県", "千葉", "千葉県", "京都府", "長野県",
        "宮城県", "山梨県", "神奈川県", "栃木県", "富山県", "愛媛県", "新潟県", "奈良県",
        "岡山県", "長崎県", "山形県", "鹿児島県", "福岡県", "福井県", "大分県", "秋田県",
        "福島県", "鳥取県", "岩手県", "熊本県", "山口県", "香川県", "群馬県", "石川県",
        "佐賀県", "青森県", "宮崎県", "徳島県", "981-0504", "山梨", "大阪", "京都市",
        "新潟", "カンボジア王国", "81", "鹿児島", "和歌山", "24", "兵庫県兵庫県",
        "福岡県福岡県", "栃木県那須郡那須町大字湯本212",
    }


def test_client_master_schema_read_only_properties_are_not_writable() -> None:
    read_only_names = [
        "最終アクション日",
        "メルアポ",
        "テレアポ",
        "アクション担当",
        "決済者名",
        "作成日時",
        "メールアドレス",
        "本社所在地",
        "【営業部】営業ステータス",
        "案件作成",
        "本社アプローチ状況",
        "担当者",
        "運営会社",
        "担当者（アクション時）",
        "先方担当者",
        "共有メモ",
        "【営業部】案件ベース_アクション履歴",
        "【営業部】提案済みサービス",
        "電話番号",
    ]
    for name in read_only_names:
        prop = CLIENT_MASTER_SCHEMA.get_property(name)
        assert prop.is_writable is False, f"{name} should be read-only"


def test_client_master_schema_sales_status_is_rollup_not_directly_editable() -> None:
    """取引先マスター側に直接編集可能な営業ステータス列が無いという設計意図を検証する。

    「【営業部】営業ステータス」はrollup型（案件管理DBから自動集計）であり、
    取引先マスター単体で手動編集する手段は存在しない。
    """
    prop = CLIENT_MASTER_SCHEMA.get_property("【営業部】営業ステータス")
    assert prop.property_type == PropertyType.ROLLUP
    assert prop.is_writable is False
