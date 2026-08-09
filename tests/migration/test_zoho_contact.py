from src.migration.zoho_contact import transform_zoho_contact


def test_transform_zoho_contact_maps_expected_fields() -> None:
    record = {
        "データID": "zcrm_123",
        "氏名": "遠藤 正巳",
        "部署名": "経営戦略本部",
        "役職": "取締役",
        "メール": "endo@example.com",
        "携帯電話": "090-1111-2222",
        "TEL会社": "03-1234-5678",
        "【Eight】会社名": "H.I.S.ホテルホールディングス株式会社",
        "名刺交換日": "2025-10-07",
        "【Eight】名刺交換者": "國方勇樹",
    }

    result = transform_zoho_contact(record)

    assert result == {
        "zoho_ID": "zcrm_123",
        "名前": "遠藤 正巳",
        "部署": "経営戦略本部",
        "役職": "取締役",
        "メールアドレス": "endo@example.com",
        "携帯番号": "090-1111-2222",
        "直通TEL": "03-1234-5678",
        "_会社名": "H.I.S.ホテルホールディングス株式会社",
    }


def test_transform_zoho_contact_falls_back_to_e_mail_column() -> None:
    """「メール」列が空で「e-mail」列にのみ値がある実データケースに対応する。"""
    record = {"データID": "zcrm_456", "氏名": "中村 崇", "e-mail": "tk-nakamura@example.com"}

    result = transform_zoho_contact(record)

    assert result["メールアドレス"] == "tk-nakamura@example.com"


def test_transform_zoho_contact_does_not_include_eight_reserved_properties() -> None:
    """名刺交換日・名刺交換者・Eight連携ID・人事異動フラグは、保留中のEight連携機能
    （タスク#37）専用に設計されたプロパティのため、Zoho移行では書き込まない
    （金沢さん確認済み）。"""
    record = {
        "データID": "zcrm_789",
        "氏名": "木村 会美子",
        "名刺交換日": "2025-05-02",
        "【Eight】名刺交換者": "誰か",
    }

    result = transform_zoho_contact(record)

    assert "名刺交換日" not in result
    assert "名刺交換者" not in result
    assert "Eight連携ID" not in result
    assert "人事異動フラグ" not in result


def test_transform_zoho_contact_missing_optional_fields_become_none() -> None:
    record = {"データID": "zcrm_999", "氏名": "個人A"}

    result = transform_zoho_contact(record)

    assert result["部署"] is None
    assert result["役職"] is None
    assert result["メールアドレス"] is None
    assert result["携帯番号"] is None
    assert result["直通TEL"] is None
    assert result["_会社名"] is None
