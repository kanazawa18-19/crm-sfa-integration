from src.migration.zoho_action import classify_zoho_action_type, transform_zoho_action


def test_classify_zoho_action_type_teleapo_keywords() -> None:
    assert classify_zoho_action_type("テレアポ") == "テレアポ"
    assert classify_zoho_action_type("テレアポ↓（大野）") == "テレアポ"
    assert classify_zoho_action_type("【電話】4回目") == "テレアポ"
    assert classify_zoho_action_type("電話") == "テレアポ"
    # プレフィックスでなく部分一致で判定するため、「【テレアポ】」のような角括弧付きにも対応。
    assert classify_zoho_action_type("【テレアポ】1回目") == "テレアポ"


def test_classify_zoho_action_type_mail_keywords() -> None:
    """メルアポ(メールでアポを取る行為)は「メール」へ対応させる方針(金沢さん確認済み)。"""
    assert classify_zoho_action_type("メルアポ↓") == "メール"
    assert classify_zoho_action_type("お礼メール") == "メール"


def test_classify_zoho_action_type_visit_and_online_keywords_are_explicit() -> None:
    """「訪問」「WEB」等、テキストから明確に判別できる場合は素直にその種別へ分類する
    （区別できない「【商談】N回目」のような曖昧なケースのみその他に寄せる）。"""
    assert classify_zoho_action_type("訪問商談") == "訪問商談"
    assert classify_zoho_action_type("商談（訪問）") == "訪問商談"
    assert classify_zoho_action_type("WEB商談") == "オンライン商談"
    assert classify_zoho_action_type("web商談") == "オンライン商談"
    assert classify_zoho_action_type("商談（WEB）") == "オンライン商談"


def test_classify_zoho_action_type_ambiguous_meeting_falls_back_to_other() -> None:
    """訪問かオンラインかテキストからは区別できないため「その他」に寄せる
    (実際の登録名自体はtitleにそのまま反映されるため情報は失われない、金沢さん確認済み)。"""
    assert classify_zoho_action_type("【商談】1回目") == "その他"
    assert classify_zoho_action_type("商談2回目") == "その他"


def test_classify_zoho_action_type_cold_call_and_inquiry() -> None:
    assert classify_zoho_action_type("飛び込み") == "飛び込み"
    assert classify_zoho_action_type("お問合せ") == "問い合わせメール"


def test_classify_zoho_action_type_unknown_falls_back_to_other() -> None:
    assert classify_zoho_action_type("LINE") == "その他"
    assert classify_zoho_action_type("") == "その他"
    assert classify_zoho_action_type(None) == "その他"


def test_transform_zoho_action_maps_expected_fields() -> None:
    record = {
        "データID": "zcrm_123",
        "アクション名": "テレアポ↓（大野）",
        "アクション日": "2025-08-04",
        "履歴メモ": "不在のため改めて連絡",
        "先方担当者": "中島様",
        "取引先.id": "zcrm_456",
        "【Notion】取引先マスター": "",
        "案件名": "",
    }

    result = transform_zoho_action(record)

    assert result == {
        "zoho_Act_ID": "zcrm_123",
        "商談回数・電話回数・メール回数（何回目）": "テレアポ↓（大野）",
        "アクション種別": "テレアポ",
        "アクション日": "2025-08-04",
        "履歴メモ": "不在のため改めて連絡",
        "先方担当者": "中島様",
        "_取引先_zoho_id": "zcrm_456",
        "_取引先_notion_page_id": None,
        "_案件_notion_page_id": None,
    }


def test_transform_zoho_action_extracts_embedded_notion_page_ids() -> None:
    record = {
        "データID": "zcrm_789",
        "アクション名": "【電話】1回目",
        "取引先.id": "",
        "【Notion】取引先マスター": "裾野セントラルホテル寿々木 (https://www.notion.so/5fbae3fd718f49e98eeb83aa10c880ea?pvs=21)",
        "案件名": "ホテル ラ フォレスタ (https://www.notion.so/518be8be9ba3492caf37affb4fa4acb6?pvs=21)",
    }

    result = transform_zoho_action(record)

    assert result["_取引先_zoho_id"] is None
    assert result["_取引先_notion_page_id"] == "5fbae3fd718f49e98eeb83aa10c880ea"
    assert result["_案件_notion_page_id"] == "518be8be9ba3492caf37affb4fa4acb6"


def test_transform_zoho_action_missing_optional_fields_become_none() -> None:
    record = {"データID": "zcrm_999", "アクション名": "テレアポ"}

    result = transform_zoho_action(record)

    assert result["アクション日"] is None
    assert result["履歴メモ"] is None
    assert result["先方担当者"] is None
    assert result["_取引先_zoho_id"] is None
    assert result["_取引先_notion_page_id"] is None
    assert result["_案件_notion_page_id"] is None
