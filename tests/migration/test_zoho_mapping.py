from src.migration.zoho_mapping import (
    transform_common_timestamps,
    transform_zoho_client_master,
    transform_zoho_project_relations,
)


def test_transform_zoho_client_master() -> None:
    assert transform_zoho_client_master({"データID": "zoho-abc-123"}) == {"Zoho_ID": "zoho-abc-123"}


def test_transform_zoho_project_relations() -> None:
    record = {
        "取引先名.id": "zoho-client-1",
        "連絡先名.id": "zoho-contact-1",
        "提案サービス": "リピッテ,メイリー",
    }

    result = transform_zoho_project_relations(record)

    assert result == {
        "_取引先Zoho_ID": "zoho-client-1",
        "_連絡先Zoho_ID": "zoho-contact-1",
        "_提案サービス名リスト": ["リピッテ", "メイリー"],
    }


def test_transform_zoho_project_relations_missing_fields() -> None:
    result = transform_zoho_project_relations({})

    assert result == {
        "_取引先Zoho_ID": None,
        "_連絡先Zoho_ID": None,
        "_提案サービス名リスト": [],
    }


def test_transform_common_timestamps() -> None:
    record = {"作成日時": "2026-08-01T10:00:00Z", "更新日時": "2026-08-02T10:00:00Z"}

    assert transform_common_timestamps(record) == {
        "created_at": "2026-08-01T10:00:00Z",
        "updated_at": "2026-08-02T10:00:00Z",
    }


def test_transform_common_timestamps_missing_fields() -> None:
    assert transform_common_timestamps({}) == {"created_at": None, "updated_at": None}
