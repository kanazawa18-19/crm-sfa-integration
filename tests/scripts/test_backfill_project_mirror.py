"""scripts/backfill_project_mirror.py（初回バックフィルCLI）の検証。

実際のNotion API・Postgresへは一切アクセスしない
（`HttpNotionClient`/`NotionUserDirectory`/`refresh_all_projects`をすべてフェイクへ
差し替える）。
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import backfill_project_mirror


@pytest.fixture(autouse=True)
def _stub_notion_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()`内の`HttpNotionClient()`/`NotionUserDirectory()`呼び出しが実際の
    NOTION_API_KEY検証・HTTP接続を行わないよう、単純なフェイクへ差し替える。"""
    monkeypatch.setattr(backfill_project_mirror, "HttpNotionClient", lambda *a, **kw: object())
    monkeypatch.setattr(backfill_project_mirror, "NotionUserDirectory", lambda *a, **kw: object())


def test_main_prints_synced_and_deleted_count_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        backfill_project_mirror,
        "refresh_all_projects",
        lambda **kwargs: {"synced_count": 9500, "deleted_count": 3},
    )

    backfill_project_mirror.main()

    out = capsys.readouterr().out
    assert "synced_count=9500" in out
    assert "deleted_count=3" in out
    assert "警告" not in out


def test_main_warns_instead_of_success_when_synced_count_is_too_low(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """案件管理DBは約1万件規模のため、synced_countが極端に少ない場合は成功調のメッセージ
    ではなく警告として出力すること（Notion API権限不足等の失敗を疑うべきため）。"""
    monkeypatch.setattr(
        backfill_project_mirror,
        "refresh_all_projects",
        lambda **kwargs: {"synced_count": 5, "deleted_count": 0},
    )

    backfill_project_mirror.main()

    out = capsys.readouterr().out
    assert "警告" in out
    assert "synced_count=5" in out
    assert "完了しました" not in out


def test_main_warns_when_synced_count_is_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        backfill_project_mirror,
        "refresh_all_projects",
        lambda **kwargs: {"synced_count": 0, "deleted_count": 0},
    )

    backfill_project_mirror.main()

    out = capsys.readouterr().out
    assert "警告" in out


def test_main_reports_skip_without_treating_it_as_success_or_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """既に別プロセス（夜間cron等）が実行中でロックを取得できなかった場合、成功メッセージも
    警告メッセージも出さず、スキップした旨を出力すること。"""

    def _fake_refresh(**kwargs: Any) -> dict[str, Any]:
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}

    monkeypatch.setattr(backfill_project_mirror, "refresh_all_projects", _fake_refresh)

    backfill_project_mirror.main()

    out = capsys.readouterr().out
    assert "スキップ" in out
    assert "already_running" in out
    assert "完了しました" not in out
    assert "警告" not in out


def test_main_reports_skip_when_required_properties_insufficient(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`refresh_all_projects()`が中身(必須プロパティ)の壊れを検知してsweepを中止した場合
    （skipped="insufficient_required_properties"、2026-08-26）も、行数チェックだけを見て
    成功調のメッセージを出さないこと（今回の事故の再発防止）。"""

    def _fake_refresh(**kwargs: Any) -> dict[str, Any]:
        return {
            "synced_count": 10000,
            "deleted_count": 0,
            "skipped": "insufficient_required_properties",
            "required_property_fill_ratios": {"案件名": 1.0, "営業ステータス": 0.0},
        }

    monkeypatch.setattr(backfill_project_mirror, "refresh_all_projects", _fake_refresh)

    backfill_project_mirror.main()

    out = capsys.readouterr().out
    assert "スキップ" in out
    assert "insufficient_required_properties" in out
    assert "完了しました" not in out
    assert "警告" not in out
