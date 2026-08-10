from src.db_schema.project import PROJECT_SCHEMA, classify_status
from src.migration.zoho_project import transform_zoho_project

# 実データ確認済み(2026-08-10、26,012件)のZoho「ステージ」列の全28種（既存の11選択肢との
# 重複「解約」「契約」「リスケ」「失注」「Bヨミ」「Aヨミ」「トライアル」を除くと21種が新規）。
_ALL_ZOHO_STAGES = (
    "契約済",
    "失注",
    "解約（処理済み）",
    "返信なし",
    "課金前解約（処理済み）",
    "課金前解約",
    "連絡済み",
    "商談中",
    "解約",
    "受注（書類回収済み）",
    "導入完了",
    "課金確認中",
    "契約",
    "アポ獲得",
    "見積もり提出済み",
    "受注・商談完了",
    "商談済み",
    "アカウント作成待ち",
    "回答待ち",
    "リスケ",
    "再商談調整中",
    "口頭受注",
    "与件整理",
    "Bヨミ",
    "申込済",
    "提案書作成中",
    "Aヨミ",
    "トライアル",
)


def test_transform_zoho_project_status_is_raw_zoho_stage_not_compressed() -> None:
    """金沢さん方針(2026-08-10): Notionの営業ステータスをマスターにしたくないため、
    Zohoの「ステージ」を圧縮・変換せずそのまま「営業ステータス」へ反映する。"""
    result = transform_zoho_project({"データID": "1", "案件名": "A", "ステージ": "返信なし"})

    assert result["営業ステータス"] == "返信なし"


def test_all_zoho_stages_are_valid_project_schema_options() -> None:
    """Zohoの生の19種が全てPROJECT_SCHEMAの選択肢として登録済みであることを保証する
    回帰テスト(Notion API側にも同じ選択肢を追加済み、2026-08-10)。"""
    valid_options = PROJECT_SCHEMA.get_property("営業ステータス").options
    for stage in _ALL_ZOHO_STAGES:
        assert stage in valid_options, f"{stage!r} is missing from PROJECT_SCHEMA options"


def test_all_zoho_stages_are_classifiable_by_classify_status() -> None:
    """Zohoの生の19種が全てclassify_status()で分類できることを保証する回帰テスト
    （ダッシュボード集計がValueErrorで落ちないようにするため）。"""
    for stage in _ALL_ZOHO_STAGES:
        classify_status(stage)  # 未知の値であればValueErrorが送出される


def test_classify_status_zoho_confirmed_values() -> None:
    """「導入完了」「受注（書類回収済み）」「受注・商談完了」「申込済」は契約扱い
    （2026-08-10金沢さん確認済み）。"""
    for stage in ("契約済", "導入完了", "受注（書類回収済み）", "受注・商談完了", "申込済"):
        assert classify_status(stage) == "契約済"


def test_classify_status_zoho_active_values_not_marked_as_confirmed() -> None:
    """「アカウント作成待ち」「課金確認中」「口頭受注」「与件整理」「再商談調整中」
    「提案書作成中」等は、契約扱いに明示指定されなかったため進行中扱い
    （2026-08-10金沢さん確認済み: 「口頭受注は契約前」）。"""
    for stage in (
        "アカウント作成待ち",
        "課金確認中",
        "返信なし",
        "商談中",
        "口頭受注",
        "与件整理",
        "再商談調整中",
        "提案書作成中",
    ):
        assert classify_status(stage) == "進行中"


def test_classify_status_zoho_cancelled_values() -> None:
    for stage in ("解約（処理済み）", "課金前解約（処理済み）", "課金前解約"):
        assert classify_status(stage) == "解約"


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
        "営業ステータス": "契約済",
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
    assert result["営業ステータス"] is None
    assert result["_サービス名リスト"] == []
    assert result["_取引先_zoho_id"] is None
    assert result["_取引先_notion_page_id"] is None
