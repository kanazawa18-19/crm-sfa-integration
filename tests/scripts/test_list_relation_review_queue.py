"""scripts/list_relation_review_queue.py（RelationReviewQueue一覧CLI）の検証。

実際のPostgresへは一切アクセスしない（`list_pending_reviews`をフェイクへ差し替える）。
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import list_relation_review_queue


def test_main_prints_no_pending_message_when_queue_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_relation_review_queue, "list_pending_reviews", lambda: [])

    list_relation_review_queue.main()

    out = capsys.readouterr().out
    assert "ありません" in out


def test_main_prints_each_pending_review_with_source_and_candidates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reviews: list[dict[str, Any]] = [
        {
            "id": "row-1",
            "sourceTool": "kintone",
            "sourceRecordId": "77",
            "targetDbKey": "client_master",
            "rawValue": "曖昧な会社名",
            "candidateNotionPageIds": ["page-1", "page-2"],
            "candidateRawNames": ["テスト商事株式会社", "テスト商事(別法人)"],
            "createdAt": "2026-08-25T00:00:00Z",
        },
        {
            "id": "row-2",
            "sourceTool": "kintone",
            "sourceRecordId": "88",
            "targetDbKey": "client_master",
            "rawValue": "存在しない会社",
            "candidateNotionPageIds": [],
            "candidateRawNames": [],
            "createdAt": "2026-08-25T01:00:00Z",
        },
    ]
    monkeypatch.setattr(list_relation_review_queue, "list_pending_reviews", lambda: reviews)

    list_relation_review_queue.main()

    out = capsys.readouterr().out
    assert "2件" in out
    assert "row-1" in out
    assert "row-2" in out
    assert "曖昧な会社名" in out
    assert "存在しない会社" in out
    assert "候補2件" in out
    assert "候補なし" in out
    # shirokuma-sec/obasan-qualityレビューWARN対応（2026-08-25）: page IDの羅列だけでなく
    # 候補の実体（会社名）も表示されること。
    assert "テスト商事株式会社(page-1)" in out
    assert "テスト商事(別法人)(page-2)" in out


def test_main_falls_back_gracefully_when_candidate_raw_names_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """本カラム追加前に記録された既存行（candidateRawNamesが空/未設定）でも、page IDを
    落とさず表示すること。"""
    reviews: list[dict[str, Any]] = [
        {
            "id": "row-1",
            "sourceTool": "kintone",
            "sourceRecordId": "77",
            "targetDbKey": "client_master",
            "rawValue": "曖昧な会社名",
            "candidateNotionPageIds": ["page-1", "page-2"],
            "candidateRawNames": [],
            "createdAt": "2026-08-25T00:00:00Z",
        },
    ]
    monkeypatch.setattr(list_relation_review_queue, "list_pending_reviews", lambda: reviews)

    list_relation_review_queue.main()

    out = capsys.readouterr().out
    assert "page-1" in out
    assert "page-2" in out
    assert "(名称不明)(page-1)" in out
