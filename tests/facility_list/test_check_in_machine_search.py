"""チェックイン機のWEB検索判定の検証。Claude APIは呼ばない(全てダミーに差し替える)。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.facility_list.domain.models import (
    CheckInMachineSource,
    CheckInMachineStatus,
    Facility,
)
from src.facility_list.infrastructure import check_in_machine_search as module


def _message(payload: dict, *, stop_reason: str = "end_turn", with_noise: bool = False):
    """Claude APIの応答を模したオブジェクト。

    `with_noise=True` のときは、テキストより前にサーバーツールの結果ブロックを置く
    （`content[0].text` 決め打ちだと壊れることを確かめるため）。
    """
    blocks = []
    if with_noise:
        blocks.append(SimpleNamespace(type="web_search_tool_result", content=[]))
    blocks.append(SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False)))
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


@pytest.fixture
def facility() -> Facility:
    return Facility(hotel_no=1, name="テストホテル", prefecture="鳥取県", city="米子市")


@pytest.fixture
def _with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _stub_client(monkeypatch: pytest.MonkeyPatch, message) -> dict:
    """`anthropic.Anthropic()` を差し替え、渡されたリクエストを記録して返す。"""
    captured: dict = {}

    class _Messages:
        def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return message

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(module, "anthropic", SimpleNamespace(Anthropic=lambda: _Client()), raising=False)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", SimpleNamespace(Anthropic=lambda: _Client()))
    return captured


class TestSearchCheckInMachine:
    def test_APIキーが無ければ検索せず未実行として返す(self, facility: Facility, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.UNKNOWN
        assert result.searched is False
        assert "ANTHROPIC_API_KEY" in (result.not_searched_reason or "")

    def test_導入ありと根拠を読み取る(self, facility, monkeypatch, _with_api_key) -> None:
        _stub_client(
            monkeypatch,
            _message(
                {
                    "status": "yes",
                    "evidence": "公式サイトに自動チェックイン機の案内がある",
                    "evidence_url": "https://example.com/checkin",
                }
            ),
        )
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.YES
        assert result.source is CheckInMachineSource.WEB_SEARCH
        assert result.evidence_url == "https://example.com/checkin"

    def test_テキストブロックが先頭でなくても読める(self, facility, monkeypatch, _with_api_key) -> None:
        _stub_client(
            monkeypatch,
            _message(
                {"status": "no", "evidence": "対面チェックインのみと明記", "evidence_url": ""},
                with_noise=True,
            ),
        )
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.NO
        assert result.evidence_url is None

    def test_根拠のない断定は不明に倒す(self, facility, monkeypatch, _with_api_key) -> None:
        _stub_client(
            monkeypatch, _message({"status": "yes", "evidence": "", "evidence_url": ""})
        )
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.UNKNOWN
        assert result.source is CheckInMachineSource.NONE

    def test_応答が切れていたら未実行として返す(self, facility, monkeypatch, _with_api_key) -> None:
        _stub_client(monkeypatch, _message({"status": "yes"}, stop_reason="max_tokens"))
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.UNKNOWN
        assert result.searched is False

    def test_壊れたJSONでも例外にしない(self, facility, monkeypatch, _with_api_key) -> None:
        broken = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="{壊れている")], stop_reason="end_turn"
        )
        _stub_client(monkeypatch, broken)
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.UNKNOWN
        assert result.searched is False

    def test_API例外でも止まらない(self, facility, monkeypatch, _with_api_key) -> None:
        class _Messages:
            def create(self, **kwargs):  # noqa: ANN003
                raise RuntimeError("接続できない")

        class _Client:
            messages = _Messages()

        monkeypatch.setitem(
            __import__("sys").modules, "anthropic", SimpleNamespace(Anthropic=lambda: _Client())
        )
        result = module.search_check_in_machine(facility)
        assert result.status is CheckInMachineStatus.UNKNOWN
        assert "接続できない" in (result.not_searched_reason or "")

    def test_Haikuと基本版のweb_searchを使う(self, facility, monkeypatch, _with_api_key) -> None:
        # `web_search_20260209` はHaikuでは400になるため、基本版を使っていること。
        captured = _stub_client(
            monkeypatch,
            _message({"status": "unknown", "evidence": "", "evidence_url": ""}),
        )
        module.search_check_in_machine(facility)
        assert captured["model"] == "claude-haiku-4-5"
        assert captured["tools"][0]["type"] == "web_search_20250305"
        assert captured["output_config"]["format"]["type"] == "json_schema"
