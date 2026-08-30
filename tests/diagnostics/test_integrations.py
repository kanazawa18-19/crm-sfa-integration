"""外部連携の疎通診断のテスト（2026-08-31）。

固定したいのは4点。
1. **未設定と失敗を混ぜない。**未設定を異常として通知すると誤報になり、
   誤報を鳴らし続けると本物の通知も無視されるようになる。
2. **1つ落ちても残りを続ける。**「全部見る」のが目的なので、最初の失敗で止まると
   2つ目以降の状態が分からなくなる。
3. **スプレッドシートはシート名の実在まで見る。**認証が通ることと書き込み先があることは別。
4. **advisory lockは必ず解放する。**握ったままにすると本番の夜間バッチを止める。
"""

from __future__ import annotations

import pytest

from src.diagnostics import integrations
from src.diagnostics.integrations import (
    FAILED,
    NOT_CONFIGURED,
    OK,
    ProbeResult,
    run_integration_diagnostics,
)


def test_未設定と失敗を別のキーに分ける() -> None:
    """`failed`には対処が要るものだけが入り、未設定は含まれない。"""
    probes = (
        ("a", lambda: ProbeResult("a", OK)),
        ("b", lambda: ProbeResult("b", NOT_CONFIGURED, detail="環境変数が無い")),
        ("c", lambda: ProbeResult("c", FAILED, detail="繋がらない")),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics()

    assert report["ok"] == ["a"]
    assert report["not_configured"] == ["b"]
    assert report["failed"] == ["c"]


def test_1つの診断が例外を投げても他の診断は続行する() -> None:
    """診断は「全部見る」のが目的なので、途中の例外で打ち切らない。"""

    def 落ちる() -> ProbeResult:
        raise RuntimeError("接続できません")

    probes = (
        ("落ちる方", 落ちる),
        ("生きてる方", lambda: ProbeResult("生きてる方", OK)),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics()

    assert report["failed"] == ["落ちる方"]
    assert report["ok"] == ["生きてる方"]
    # 例外の種類と本文が結果に残ること（ログを見に行かなくても原因が分かるように）。
    落ちた = next(r for r in report["results"] if r["name"] == "落ちる方")
    assert "RuntimeError" in 落ちた["detail"]
    assert "接続できません" in 落ちた["detail"]


def test_onlyで診断対象を絞れる() -> None:
    probes = (
        ("a", lambda: ProbeResult("a", OK)),
        ("b", lambda: ProbeResult("b", OK)),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics(only=("b",))

    assert [r["name"] for r in report["results"]] == ["b"]


def test_全ての診断結果に所要時間が入る() -> None:
    """`rc=0`でも想定より遅ければどこかで空回りしている。所要時間を必ず残す。"""
    probes = (("a", lambda: ProbeResult("a", OK)),)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics()

    assert "elapsed_ms" in report["results"][0]
    assert report["results"][0]["elapsed_ms"] >= 0


class _シート名を返すだけのクライアント:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_sheet_names(self) -> tuple[str, list[str]]:
        return "テスト用スプレッドシート", self._names


def _スプレッドシート診断(names: list[str], monkeypatch: pytest.MonkeyPatch) -> ProbeResult:
    monkeypatch.setattr(
        "src.sync_engine.clients.spreadsheet_client.HttpSpreadsheetClient",
        lambda **kwargs: _シート名を返すだけのクライアント(names),
    )
    return integrations.probe_spreadsheet()


def test_同期先のシートが全て実在すればok(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.db_schema.registry import ALL_SCHEMAS

    全シート名 = [schema.spreadsheet_sheet_name for schema in ALL_SCHEMAS]
    result = _スプレッドシート診断(全シート名 + ["関係ないシート"], monkeypatch)

    assert result.status == OK


def test_同期先のシートが1枚でも欠けていれば失敗として報告する(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認証が通っていてもシート名が変わっていれば同期は静かに失敗し続ける。
    そこを「ok」と言わないことがこの診断の存在意義。"""
    from src.db_schema.registry import ALL_SCHEMAS

    全シート名 = [schema.spreadsheet_sheet_name for schema in ALL_SCHEMAS]
    欠けた = 全シート名[1:]
    result = _スプレッドシート診断(欠けた, monkeypatch)

    assert result.status == FAILED
    assert 全シート名[0] in result.detail


def test_スプレッドシートの認証情報が無ければ未設定として扱う(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def 構築時に落ちる(**kwargs: object) -> None:
        raise ValueError("SPREADSHEET_ID environment variable ... is required but not set")

    monkeypatch.setattr(
        "src.sync_engine.clients.spreadsheet_client.HttpSpreadsheetClient", 構築時に落ちる
    )
    result = integrations.probe_spreadsheet()

    assert result.status == NOT_CONFIGURED
