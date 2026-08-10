from src.migration.zoho_project import transform_zoho_project


def test_transform_zoho_project_maps_expected_fields() -> None:
    record = {
        "データID": "zcrm_123",
        "案件名": "サンプルホテル導入案件",
        "初期費用": "29800",
        "月額費用": "8800",
        "契約日 / 予想契約日": "2026-09-01",
        "メモ": "備考メモ",
        "サイトコントローラー": "リンカーン",
        "かつやさん": "true",
        "ネックポイント": "予算未確保",
        "失注理由": "",
        "アクション日": "2026-08-01",
        "メールアドレス": "sample@example.com",
        "電話番号": "03-1234-5678",
        "ステージ": "契約済",
        "提案サービス": "リピッテ、メイリー",
        "取引先名.id": "zcrm_456",
        "【Notion】取引先マスター": "",
    }

    result = transform_zoho_project(record)

    assert result == {
        "zoho_ID": "zcrm_123",
        "案件名": "サンプルホテル導入案件",
        "初期費用": 29800.0,
        "月額費用": 8800.0,
        "契約日 / 予想契約日": "2026-09-01",
        "メモ": "備考メモ",
        "サイトコントローラー": ["リンカーン"],
        "かつやさん": True,
        "ネックポイント": "予算未確保",
        "失注理由": None,
        "アクション日": "2026-08-01",
        "メールアドレス": "sample@example.com",
        "電話番号": "03-1234-5678",
        "_ステージ": "契約済",
        "_サービス名リスト": ["リピッテ", "メイリー"],
        "_取引先_zoho_id": "zcrm_456",
        "_取引先_notion_page_id": None,
    }


def test_transform_zoho_project_does_not_include_readonly_formula_or_rollup_properties() -> None:
    """粗利・個人粗利(FORMULA型)、予算組のタイミング・決算月(ROLLUP型)は
    Notion側で自動計算される読み取り専用プロパティのため、同名のZoho列があっても
    書き込み対象に含めない。"""
    record = {
        "データID": "zcrm_789",
        "案件名": "テスト案件",
        "粗利": "100000",
        "個人粗利": "50000",
        "予算組のタイミング": "4月",
        "決算月": "3月",
    }

    result = transform_zoho_project(record)

    assert "粗利" not in result
    assert "個人粗利" not in result
    assert "予算組のタイミング" not in result
    assert "決算月" not in result


def test_transform_zoho_project_boolean_field_parses_true_false_strings_correctly() -> None:
    """Python の bool("false") は True になってしまうため、文字列比較で判定する必要がある
    ことの回帰確認。"""
    assert transform_zoho_project({"データID": "1", "案件名": "A", "かつやさん": "false"})["かつやさん"] is False
    assert transform_zoho_project({"データID": "2", "案件名": "B", "かつやさん": "true"})["かつやさん"] is True
    assert transform_zoho_project({"データID": "3", "案件名": "C"})["かつやさん"] is False


def test_transform_zoho_project_site_controller_empty_becomes_empty_list() -> None:
    result = transform_zoho_project({"データID": "1", "案件名": "A"})

    assert result["サイトコントローラー"] == []


def test_transform_zoho_project_extracts_embedded_notion_client_page_id() -> None:
    record = {
        "データID": "zcrm_999",
        "案件名": "テスト案件2",
        "取引先名.id": "",
        "【Notion】取引先マスター": "サンプルホテル (https://www.notion.so/5fbae3fd718f49e98eeb83aa10c880ea?pvs=21)",
    }

    result = transform_zoho_project(record)

    assert result["_取引先_zoho_id"] is None
    assert result["_取引先_notion_page_id"] == "5fbae3fd718f49e98eeb83aa10c880ea"


def test_transform_zoho_project_missing_optional_fields_become_none() -> None:
    record = {"データID": "zcrm_000", "案件名": "最小構成案件"}

    result = transform_zoho_project(record)

    assert result["初期費用"] is None
    assert result["月額費用"] is None
    assert result["契約日 / 予想契約日"] is None
    assert result["メモ"] is None
    assert result["ネックポイント"] is None
    assert result["失注理由"] is None
    assert result["アクション日"] is None
    assert result["メールアドレス"] is None
    assert result["電話番号"] is None
    assert result["_ステージ"] is None
    assert result["_サービス名リスト"] == []
    assert result["_取引先_zoho_id"] is None
    assert result["_取引先_notion_page_id"] is None
