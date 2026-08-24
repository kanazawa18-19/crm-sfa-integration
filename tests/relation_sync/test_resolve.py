"""src/relation_sync/resolve.py（リレーション解決）の検証。

`find_by_normalized_name`/`enqueue_for_review`（実際のPostgresアクセス）はmonkeypatchで
差し替え、実際のDB接続は発生させない。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.relation_sync import resolve


@pytest.fixture(autouse=True)
def _enable_relation_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定では`RELATION_SYNC_ENABLED`が無効(未設定)のため、本ファイルの大半のテストでは
    明示的に有効化する（フラグ自体の挙動を検証するテストは個別にdelenv/上書きする）。"""
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")


@pytest.fixture
def _enqueue_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        resolve, "enqueue_for_review", lambda **kwargs: calls.append(kwargs)
    )
    return calls


def test_resolve_client_master_relation_returns_page_id_for_single_match(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        resolve,
        "find_by_normalized_name",
        lambda normalized: [{"notion_page_id": "page-1", "raw_name": "テスト商事株式会社"}],
    )

    result = resolve.resolve_client_master_relation(
        "テスト商事株式会社", source_tool="kintone", source_record_id="77"
    )

    assert result == "page-1"
    assert _enqueue_calls == []


def test_resolve_client_master_relation_normalizes_before_lookup(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    lookups: list[str] = []
    monkeypatch.setattr(
        resolve,
        "find_by_normalized_name",
        lambda normalized: lookups.append(normalized) or [{"notion_page_id": "page-1", "raw_name": "x"}],
    )

    # 全角/半角・法人格表記ゆれをnormalize_company_name_strong()で吸収して検索すること。
    resolve.resolve_client_master_relation(
        "テスト商事　株式会社", source_tool="kintone", source_record_id="77"
    )

    assert lookups == ["テスト商事"]


def test_resolve_client_master_relation_enqueues_for_review_when_no_match(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(resolve, "find_by_normalized_name", lambda normalized: [])

    result = resolve.resolve_client_master_relation(
        "存在しない会社", source_tool="kintone", source_record_id="77"
    )

    assert result is None
    assert _enqueue_calls == [
        {
            "source_tool": "kintone",
            "source_record_id": "77",
            "target_db_key": "client_master",
            "raw_value": "存在しない会社",
            "candidate_notion_page_ids": [],
            "candidate_raw_names": [],
        }
    ]


def test_resolve_client_master_relation_enqueues_for_review_when_ambiguous(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        resolve,
        "find_by_normalized_name",
        lambda normalized: [
            {"notion_page_id": "page-1", "raw_name": "テスト商事"},
            {"notion_page_id": "page-2", "raw_name": "テスト商事"},
        ],
    )

    result = resolve.resolve_client_master_relation(
        "テスト商事", source_tool="zoho", source_record_id="zoho-1"
    )

    assert result is None
    assert _enqueue_calls == [
        {
            "source_tool": "zoho",
            "source_record_id": "zoho-1",
            "target_db_key": "client_master",
            "raw_value": "テスト商事",
            "candidate_notion_page_ids": ["page-1", "page-2"],
            "candidate_raw_names": ["テスト商事", "テスト商事"],
        }
    ]


def test_resolve_client_master_relation_preserves_raw_names_paired_with_candidate_ids(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    """shirokuma-sec/obasan-qualityレビューWARN対応（2026-08-25）: 候補のraw_nameを
    candidate_notion_page_idsと同じ順序でcandidate_raw_namesへ渡し、
    scripts/list_relation_review_queue.pyがpage IDだけでなく会社名も表示できるようにする。"""
    monkeypatch.setattr(
        resolve,
        "find_by_normalized_name",
        lambda normalized: [
            {"notion_page_id": "page-1", "raw_name": "テスト商事株式会社"},
            {"notion_page_id": "page-2", "raw_name": "テスト商事(別法人)"},
        ],
    )

    resolve.resolve_client_master_relation(
        "テスト商事", source_tool="kintone", source_record_id="77"
    )

    assert _enqueue_calls[0]["candidate_notion_page_ids"] == ["page-1", "page-2"]
    assert _enqueue_calls[0]["candidate_raw_names"] == [
        "テスト商事株式会社",
        "テスト商事(別法人)",
    ]


def test_resolve_client_master_relation_returns_none_without_enqueue_for_blank_name(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    lookup_calls: list[str] = []
    monkeypatch.setattr(
        resolve, "find_by_normalized_name", lambda normalized: lookup_calls.append(normalized) or []
    )

    assert resolve.resolve_client_master_relation(
        "", source_tool="kintone", source_record_id="77"
    ) is None
    assert resolve.resolve_client_master_relation(
        "   ", source_tool="kintone", source_record_id="77"
    ) is None

    assert lookup_calls == []
    assert _enqueue_calls == []


# --- RELATION_SYNC_ENABLED gate (2026-08-25、shirokuma-sec/obasan-qualityレビューBLOCKER対応) ---
# ClientNameIndexへの投入経路(Webhook/夜間reconciliation)が無効化されている間、この関数だけが
# 常時(フラグ無しで)呼ばれ続けるため、関数自身がフラグを見て完全にno-opになることを確認する。


def test_resolve_client_master_relation_is_noop_when_flag_unset(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.delenv("RELATION_SYNC_ENABLED", raising=False)
    lookup_calls: list[str] = []
    monkeypatch.setattr(
        resolve, "find_by_normalized_name", lambda normalized: lookup_calls.append(normalized) or []
    )

    result = resolve.resolve_client_master_relation(
        "テスト商事株式会社", source_tool="kintone", source_record_id="77"
    )

    assert result is None
    assert lookup_calls == []  # ClientNameIndexへ問い合わせ自体を行わない
    assert _enqueue_calls == []  # RelationReviewQueueへも記録しない


def test_resolve_client_master_relation_is_noop_when_flag_is_not_exactly_true(
    monkeypatch: pytest.MonkeyPatch, _enqueue_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "1")
    lookup_calls: list[str] = []
    monkeypatch.setattr(
        resolve, "find_by_normalized_name", lambda normalized: lookup_calls.append(normalized) or []
    )

    result = resolve.resolve_client_master_relation(
        "テスト商事株式会社", source_tool="kintone", source_record_id="77"
    )

    assert result is None
    assert lookup_calls == []
