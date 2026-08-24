"""scripts/backfill_client_name_index.py（初回バックフィルCLI）の検証。

実際のNotion API・Postgresへは一切アクセスしない
（`HttpNotionClient`/`refresh_all_client_names`をすべてフェイクへ差し替える）。
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import backfill_client_name_index


@pytest.fixture(autouse=True)
def _stub_notion_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()`内の`HttpNotionClient()`呼び出しが実際のNOTION_API_KEY検証・HTTP接続を
    行わないよう、単純なフェイクへ差し替える。"""
    monkeypatch.setattr(backfill_client_name_index, "HttpNotionClient", lambda *a, **kw: object())


def test_main_prints_synced_and_deleted_count_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        backfill_client_name_index,
        "refresh_all_client_names",
        lambda **kwargs: {"synced_count": 9914, "deleted_count": 3},
    )

    backfill_client_name_index.main()

    out = capsys.readouterr().out
    assert "synced_count=9914" in out
    assert "deleted_count=3" in out
    assert "警告" not in out


def test_main_warns_instead_of_success_when_synced_count_is_too_low(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """取引先マスターDBは約1万件規模のため、synced_countが極端に少ない場合は成功調の
    メッセージではなく警告として出力すること（Notion API権限不足等の失敗を疑うべきため）。"""
    monkeypatch.setattr(
        backfill_client_name_index,
        "refresh_all_client_names",
        lambda **kwargs: {"synced_count": 5, "deleted_count": 0},
    )

    backfill_client_name_index.main()

    out = capsys.readouterr().out
    assert "警告" in out
    assert "synced_count=5" in out
    assert "完了しました" not in out


def test_main_warns_when_synced_count_is_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        backfill_client_name_index,
        "refresh_all_client_names",
        lambda **kwargs: {"synced_count": 0, "deleted_count": 0},
    )

    backfill_client_name_index.main()

    out = capsys.readouterr().out
    assert "警告" in out


def test_main_reports_skip_without_treating_it_as_success_or_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """既に別プロセス（夜間cron等）が実行中でロックを取得できなかった場合、成功メッセージも
    警告メッセージも出さず、スキップした旨を出力すること。"""

    def _fake_refresh(**kwargs: Any) -> dict[str, Any]:
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}

    monkeypatch.setattr(backfill_client_name_index, "refresh_all_client_names", _fake_refresh)

    backfill_client_name_index.main()

    out = capsys.readouterr().out
    assert "スキップ" in out
    assert "already_running" in out
    assert "完了しました" not in out
    assert "警告" not in out
