"""src/relation_sync/resolve_zoho.py（Zohoアクション履歴の取引先マスターリレーション解決）の検証。

`resolve_client_master_relation`（実際のPostgresアクセスを伴う）はmonkeypatchで差し替え、
実際のDB接続は発生させない。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.relation_sync import resolve_zoho as module
from src.relation_sync.resolve_zoho import extract_zoho_lookup_name

from src.relation_sync import resolve_zoho


@pytest.fixture(autouse=True)
def _enable_relation_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定では`RELATION_SYNC_ENABLED`が無効(未設定)のため、本ファイルの大半のテストでは
    明示的に有効化する（フラグ自体の挙動を検証するテストは個別にdelenv/上書きする）。"""
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")


class _FakeZohoClient:
    def __init__(self, records: dict[str, dict[str, Any] | None]) -> None:
        self._records = records
        self.get_record_calls: list[tuple[str, str]] = []

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        self.get_record_calls.append((module, record_id))
        return self._records.get(record_id)


class _FailingZohoClient:
    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        raise RuntimeError("zoho api unavailable")


def test_resolves_via_embedded_hint_in_delta_without_calling_zoho_client_or_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """field22が今回のWebhook通知に含まれ、埋め込みヒントを持つ場合はそのまま使う
    （Zoho APIでのレコード全体取得も、名寄せ解決も一切行わない）。"""
    resolver_calls: list[Any] = []
    monkeypatch.setattr(
        resolve_zoho,
        "resolve_client_master_relation",
        lambda *args, **kwargs: resolver_calls.append((args, kwargs)) or "should-not-be-used",
    )
    zoho_client = _FakeZohoClient({})

    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id="77",
        changed_values={
            "field22": "テスト商事 (https://www.notion.so/slug-0123456789abcdef0123456789abcdef?pvs=21)"
        },
        zoho_client=zoho_client,
    )

    assert result == "0123456789abcdef0123456789abcdef"
    assert zoho_client.get_record_calls == []
    assert resolver_calls == []


def test_resolves_via_embedded_hint_fetched_from_zoho_api_when_only_field6_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """field6のみが変更差分に含まれる場合、field22の現在値をZoho APIで取得して確認する。"""
    resolver_calls: list[Any] = []
    monkeypatch.setattr(
        resolve_zoho,
        "resolve_client_master_relation",
        lambda *args, **kwargs: resolver_calls.append((args, kwargs)) or "should-not-be-used",
    )
    zoho_client = _FakeZohoClient(
        {
            "77": {
                "field22": "テスト商事 (https://www.notion.so/0123456789abcdef0123456789abcdef)",
                "field6": "テスト商事",
            }
        }
    )

    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id="77",
        changed_values={"field6": "テスト商事"},
        zoho_client=zoho_client,
    )

    assert result == "0123456789abcdef0123456789abcdef"
    assert zoho_client.get_record_calls == [("CustomModule2", "77")]
    assert resolver_calls == []


def test_falls_back_to_raw_name_resolution_when_no_embedded_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """埋め込みヒントが無い場合、field6の生の会社名をresolve_client_master_relation()へ渡す。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        resolve_zoho,
        "resolve_client_master_relation",
        lambda raw_name, **kwargs: calls.append({"raw_name": raw_name, **kwargs}) or "page-1",
    )

    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id="77",
        changed_values={"field22": "", "field6": "テスト商事"},
        zoho_client=None,
    )

    assert result == "page-1"
    assert calls == [{"raw_name": "テスト商事", "source_tool": "zoho", "source_record_id": "77"}]


def test_fetches_raw_name_from_zoho_api_when_only_field22_changed_without_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """field22のみが変更差分に含まれ埋め込みヒントを持たない場合、field6の現在値をZoho APIで
    取得して名寄せ解決にフォールバックする。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        resolve_zoho,
        "resolve_client_master_relation",
        lambda raw_name, **kwargs: calls.append({"raw_name": raw_name, **kwargs}) or "page-1",
    )
    zoho_client = _FakeZohoClient({"77": {"field22": "埋め込みヒント無し", "field6": "テスト商事"}})

    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id="77",
        changed_values={"field22": "埋め込みヒント無し"},
        zoho_client=zoho_client,
    )

    assert result == "page-1"
    assert calls == [{"raw_name": "テスト商事", "source_tool": "zoho", "source_record_id": "77"}]
    # field22の判定・field6の取得のいずれもfetch_record_once()の1回のキャッシュを共有すること。
    assert zoho_client.get_record_calls == [("CustomModule2", "77")]


def test_treats_failed_zoho_fetch_as_missing_data_and_falls_back_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zoho APIでのレコード全体取得に失敗した場合、安全側に倒して空文字として扱う
    （resolve_client_master_relation()は空文字を「未入力」としてNoneを返す、レビューキュー
    への記録も行わない）。"""
    calls: list[Any] = []
    monkeypatch.setattr(
        resolve_zoho,
        "resolve_client_master_relation",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "page-1",
    )

    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id="77",
        changed_values={"field6": "テスト商事"},
        zoho_client=_FailingZohoClient(),
    )

    # field6は変更差分に含まれるためそのまま使われ、field22はfetch失敗により「値なし」扱い。
    assert result == "page-1"
    assert calls == [(("テスト商事",), {"source_tool": "zoho", "source_record_id": "77"})]


def test_returns_none_without_any_calls_when_relation_sync_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RELATION_SYNC_ENABLED", raising=False)
    resolver_calls: list[Any] = []
    monkeypatch.setattr(
        resolve_zoho,
        "resolve_client_master_relation",
        lambda *args, **kwargs: resolver_calls.append((args, kwargs)) or "page-1",
    )
    zoho_client = _FakeZohoClient({"77": {"field22": "x", "field6": "y"}})

    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id="77",
        changed_values={"field6": "テスト商事"},
        zoho_client=zoho_client,
    )

    assert result is None
    assert zoho_client.get_record_calls == []
    assert resolver_calls == []


# --- ルックアップ項目の値の取り出し（2026-08-31、本番ログで発覚した不具合） ----------------


class Test_ルックアップ項目から会社名を取り出す:
    """**Zohoのルックアップ項目は`{"name": ..., "id": ...}`という辞書で返る。**

    これをそのまま`str()`していたため、名寄せに
    `"{'name': 'ホテルユクエスタ旭橋', 'id': '...'}"` という文字列が渡り、
    **Zoho発のアクションの取引先リレーションが一度も解決できていなかった**
    （毎回レビューキューに積まれていた）。
    """

    def test_辞書からnameを取り出す(self) -> None:
        assert (
            extract_zoho_lookup_name({"name": "ホテルユクエスタ旭橋", "id": "2233"})
            == "ホテルユクエスタ旭橋"
        )

    def test_文字列はそのまま(self) -> None:
        """Webhookのdeltaでは文字列で来ることもある。"""
        assert extract_zoho_lookup_name("株式会社ABC") == "株式会社ABC"

    def test_値が無ければ空文字(self) -> None:
        assert extract_zoho_lookup_name(None) == ""
        assert extract_zoho_lookup_name({"id": "2233"}) == ""

    def test_辞書のまま名寄せに渡さない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回帰防止。解決関数へ辞書の文字列表現が渡ると必ず名寄せに失敗する。"""
        渡された: list[str] = []
        monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")
        monkeypatch.setattr(
            module,
            "resolve_client_master_relation",
            lambda raw_name, **_: (渡された.append(raw_name), None)[1],
        )

        module.resolve_zoho_action_client_master_relation(
            record_id="1",
            changed_values={"field6": {"name": "ホテルABC", "id": "9"}},
            zoho_client=None,
        )

        assert 渡された == ["ホテルABC"]
        assert "{" not in 渡された[0]
