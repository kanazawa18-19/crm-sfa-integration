import logging

import pytest

from src.migration.zoho_chain import normalize_approach_status, transform_zoho_chain


def test_normalize_approach_status_known_value_passthrough() -> None:
    assert normalize_approach_status("連絡済み（担当者未達）") == "連絡済み（担当者未達）"


def test_normalize_approach_status_empty_returns_none() -> None:
    assert normalize_approach_status("") is None
    assert normalize_approach_status(None) is None


def test_normalize_approach_status_unknown_value_falls_back_to_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """実データ確認済み(2026-08-10): 232件中「提案中」1件のみ選択肢に存在しない値。"""
    with caplog.at_level(logging.WARNING):
        result = normalize_approach_status("提案中")

    assert result is None
    assert any("提案中" in record.message for record in caplog.records)


def test_transform_zoho_chain_maps_expected_fields() -> None:
    record = {
        "データID": "zcrm_123",
        "チェーン名・グループ名": "サンプルチェーン",
        "アプローチ状況": "提案済み",
        "施設数": "5",
        "本社": "東京本社",
        "本社所在地": "東京都千代田区1-1-1",
        "運営会社": "サンプル運営株式会社",
        "電話": "03-1234-5678",
        "チェーンURL": "https://example.com",
        "メモ": "備考メモ",
        "決裁": "本部決裁",
        "未導入店へのアプローチ": "未実施",
        "自動チェックイン機（URL）": "https://checkin.example.com",
        "自動チェックイン機": "導入済み",
        "最終更新日（最終アプローチ日）": "2026-08-01",
    }

    result = transform_zoho_chain(record)

    assert result == {
        "zoho_ID": "zcrm_123",
        "グループ名": "サンプルチェーン",
        "アプローチ状況": "提案済み",
        "施設数": "5",
        "本社": "東京本社",
        "本社所在地": "東京都千代田区1-1-1",
        "運営会社": "サンプル運営株式会社",
        "電話": "03-1234-5678",
        "URL": "https://example.com",
        "メモ": "備考メモ",
        "決裁": "本部決裁",
        "未導入店舗へのアプローチ": "未実施",
        "自動チェックインURL": "https://checkin.example.com",
        "自動チェックイン": "導入済み",
        "最終アプローチ日": "2026-08-01",
    }


def test_transform_zoho_chain_missing_optional_fields_become_none() -> None:
    record = {"データID": "zcrm_456", "チェーン名・グループ名": "個人チェーン"}

    result = transform_zoho_chain(record)

    assert result["アプローチ状況"] is None
    assert result["施設数"] is None
    assert result["本社"] is None
