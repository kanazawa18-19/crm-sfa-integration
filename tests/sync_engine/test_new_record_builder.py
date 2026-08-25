"""src/sync_engine/new_record_builder.py（新規レコード作成時のプロパティ変換）の検証。

既存のWebhook部分更新用フィールド変換テーブル（KINTONE_FIELD_TRANSFORMS/
ZOHO_LABEL_FIELD_MAPPINGS）をレコード全体データに対してループ適用するだけの薄い層のため、
個々の値変換ロジック自体のテストは重複させず、「レコード全体データを渡した場合に正しく
複数フィールドがまとめて変換されること」「取引先マスターリレーションが解決できる場合と
できない場合の両方」に絞って検証する。
"""

from __future__ import annotations

import pytest

from src.db_schema.base import Tool
from src.sync_engine.new_record_builder import build_notion_properties_for_new_record


def test_build_from_kintone_client_master_record() -> None:
    raw_record = {
        "顧客名": "テスト商事",
        "顧客種別": "ホテル・旅館",
        "都道府県名": "東京都",
        "TEL": "03-1234-5678",
        # リレーション解決が必要なため意図的に対象外のフィールド（KINTONE_FIELD_TRANSFORMS
        # に存在しない）。
        "本部名": "テストチェーン",
    }

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="10",
        raw_record=raw_record,
    )

    assert properties == {
        "取引先名": "テスト商事",
        "顧客種別": "ホテル・旅館",
        "都道府県": "東京都",
        "TEL": "03-1234-5678",
    }


def test_build_from_kintone_action_resolves_client_master_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import kintone_field_transforms as module

    monkeypatch.setattr(
        module,
        "resolve_client_master_relation",
        lambda raw_name, **kwargs: "notion-page-1" if kwargs["source_record_id"] == "77" else None,
    )
    raw_record = {"client_name": "テスト商事", "comment": "折り返し予定"}

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.KINTONE, db_key="action", external_id="77", raw_record=raw_record
    )

    assert properties == {
        "履歴メモ": "折り返し予定",
        "👨‍👩‍👧‍👦 取引先マスター": "notion-page-1",
    }


def test_build_from_kintone_action_excludes_unresolved_client_master_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未解決（曖昧・候補なし）の場合、リレーションプロパティ自体を含めない（新規作成時は
    空欄のまま作成されるだけであり、既存値の上書き防止という意味合いは無い）。"""
    from src.sync_engine.webhook_handlers import kintone_field_transforms as module

    monkeypatch.setattr(module, "resolve_client_master_relation", lambda raw_name, **kwargs: None)
    raw_record = {"client_name": "曖昧な会社名", "comment": "折り返し予定"}

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.KINTONE, db_key="action", external_id="77", raw_record=raw_record
    )

    assert "👨‍👩‍👧‍👦 取引先マスター" not in properties
    assert properties["履歴メモ"] == "折り返し予定"


def test_build_from_kintone_skips_fields_not_in_transform_table() -> None:
    raw_record = {"顧客名": "テスト商事", "店舗名": "何か"}  # "店舗名"は意図的に対象外

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.KINTONE, db_key="client_master", external_id="10", raw_record=raw_record
    )

    assert properties == {"取引先名": "テスト商事"}


def test_build_from_kintone_skips_field_when_transform_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_record = {"ドロップダウン_2": "存在しないステータス", "初期費用": "500000"}

    with caplog.at_level("WARNING"):
        properties = build_notion_properties_for_new_record(
            source_tool=Tool.KINTONE, db_key="project", external_id="45", raw_record=raw_record
        )

    assert properties == {"初期費用": 500000.0}
    assert any("ドロップダウン_2" in r.getMessage() for r in caplog.records)


def test_build_from_zoho_project_record() -> None:
    raw_record = {"Deal_Name": "サンプル案件", "Stage": "商談中(B)", "field": 500000}

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.ZOHO,
        db_key="project",
        external_id="zoho-1",
        raw_record=raw_record,
    )

    assert properties == {
        "案件名": "サンプル案件",
        "営業ステータス": "商談中(B)",
        "初期費用": 500000.0,
    }


def test_build_from_zoho_action_resolves_client_master_relation_via_embedded_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """field22に埋め込みNotionページIDヒントがある場合、RelationReviewQueueへの記録や
    ClientNameIndexへの名寄せを一切経由せず、そのまま抽出して使うこと（resolve_zoho.pyの
    実装をモックせず実際に通す統合的なテスト）。"""
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")
    raw_record = {
        "Name": "【電話】4回目",
        "field22": (
            "テスト商事 (https://www.notion.so/slug-0123456789abcdef0123456789abcdef?pvs=21)"
        ),
        "field6": "テスト商事",
    }

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.ZOHO, db_key="action", external_id="zoho-action-1", raw_record=raw_record
    )

    assert properties["商談回数・電話回数・メール回数（何回目）"] == "【電話】4回目"
    assert properties["👨‍👩‍👧‍👦 取引先マスター"] == "0123456789abcdef0123456789abcdef"


def test_build_from_zoho_action_falls_back_to_name_resolution_when_no_embedded_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")
    from src.relation_sync import resolve_zoho as resolve_zoho_module

    monkeypatch.setattr(
        resolve_zoho_module,
        "resolve_client_master_relation",
        lambda raw_name, **kwargs: "notion-page-1" if raw_name == "テスト商事" else None,
    )
    raw_record = {"Name": "【電話】4回目", "field22": "", "field6": "テスト商事"}

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.ZOHO, db_key="action", external_id="zoho-action-1", raw_record=raw_record
    )

    assert properties["👨‍👩‍👧‍👦 取引先マスター"] == "notion-page-1"


def test_build_from_zoho_action_excludes_relation_when_resolution_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RELATION_SYNC_ENABLED", raising=False)
    raw_record = {"Name": "【電話】4回目", "field6": "テスト商事"}

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.ZOHO, db_key="action", external_id="zoho-action-1", raw_record=raw_record
    )

    assert "👨‍👩‍👧‍👦 取引先マスター" not in properties
    assert properties["商談回数・電話回数・メール回数（何回目）"] == "【電話】4回目"


def test_build_ignores_unknown_zoho_api_names() -> None:
    raw_record = {"id": "zoho-1", "Owner": {"id": "1"}, "Deal_Name": "サンプル案件"}

    properties = build_notion_properties_for_new_record(
        source_tool=Tool.ZOHO, db_key="project", external_id="zoho-1", raw_record=raw_record
    )

    assert properties == {"案件名": "サンプル案件"}


def test_build_raises_for_unsupported_source_tool() -> None:
    with pytest.raises(ValueError):
        build_notion_properties_for_new_record(
            source_tool=Tool.NOTION, db_key="project", external_id="x", raw_record={}
        )
