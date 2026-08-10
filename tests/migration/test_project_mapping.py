import pytest

from src.migration.project_mapping import normalize_project_status, transform_kintone_project


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("契約済", "契約"),
        ("商談中（A）", "アポ"),
        ("商談中（B）", "アポ"),
        ("商談中（C）", "アポ"),
        ("商談中（D）", "アポ"),
        ("失注", "失注"),
        ("解約", "解約"),
        (" 解約 ", "解約"),
        ("アポ", "アポ"),
        ("契約", "契約"),
    ],
)
def test_normalize_project_status_known_values(raw: str, expected: str) -> None:
    assert normalize_project_status(raw) == expected


def test_normalize_project_status_covers_all_schema_options() -> None:
    """実Notionスキーマの営業ステータス全32値（kintone由来の既存11値＋Zoho「ステージ」
    由来の21値、2026-08-10追加）を、normalize_project_status()がエラーにならず、かつ
    常に有効な選択肢へ変換できることを保証する回帰テスト。

    「契約済」はZoho「ステージ」由来の独立した選択肢として追加されたが、kintone側では
    以前から「契約」への別名（_STATUS_ALIASES）として扱われている。同じ文字列でも
    移行元システムによって意味が異なるため、素通し（恒等変換）ではなくエイリアス変換後の
    値も含めて「有効な選択肢である」ことのみを検証する。
    """
    from src.db_schema.project import PROJECT_SCHEMA

    valid_options = PROJECT_SCHEMA.get_property("営業ステータス").options
    assert len(valid_options) == 32
    for option in valid_options:
        assert normalize_project_status(option) in valid_options


def test_normalize_project_status_unknown_value_raises() -> None:
    with pytest.raises(ValueError):
        normalize_project_status("謎のステータス")


def test_normalize_project_status_none_raises_instead_of_attribute_error() -> None:
    """kintoneの空欄フィールドはNoneで返ってくることがあるため、AttributeErrorにならず
    ValueErrorとして扱われることを確認する。"""
    with pytest.raises(ValueError):
        normalize_project_status(None)


def test_transform_kintone_project_status_field_none_raises_value_error() -> None:
    record = {
        "レコード番号": "3003",
        "施設名（会社名）": "株式会社サンプル3",
        "契約進捗状況": None,
    }

    with pytest.raises(ValueError):
        transform_kintone_project(record)


def test_transform_kintone_project_contracted_sets_contract_date() -> None:
    """実データ回帰確認: kintoneの「契約済」は実Notionスキーマの「契約」へ正規化される。"""
    record = {
        "レコード番号": "3001",
        "施設名（会社名）": "株式会社サンプル",
        "契約進捗状況": "契約済",
        "課金開始予定日": "2026-09-01",
        "サービス（ショット）": "リピッテ、メイリー",
        "提案料金（ランニング）": "50000",
        "提案料金（イニシャル）": "100000",
    }

    result = transform_kintone_project(record)

    # PROJECT_SCHEMAには「契約日」「予想契約日」を分けたプロパティは存在せず、単一の
    # 「契約日 / 予想契約日」DATEプロパティに統合されている。
    assert result["営業ステータス"] == "契約"
    assert result["契約日 / 予想契約日"] == "2026-09-01"
    assert result["_サービス名リスト"] == ["リピッテ", "メイリー"]
    assert result["月額費用"] == "50000"
    assert result["初期費用"] == "100000"
    assert result["_取引先名"] == "株式会社サンプル"
    assert result["kintone_ID"] == "3001"


def test_transform_kintone_project_in_progress_sets_expected_date() -> None:
    """実データ回帰確認: kintoneの「商談中（B）」は実Notionスキーマの「アポ」へ正規化される
    （A〜Dヨミへは細分せず、まとめて「アポ」に統合する方針、2026-08-09業務判断確認済み）。"""
    record = {
        "レコード番号": "3002",
        "施設名（会社名）": "株式会社サンプル2",
        "契約進捗状況": "商談中（B）",
        "課金開始予定日": "2026-10-01",
    }

    result = transform_kintone_project(record)

    assert result["営業ステータス"] == "アポ"
    assert result["契約日 / 予想契約日"] == "2026-10-01"
    assert result["_サービス名リスト"] == []
