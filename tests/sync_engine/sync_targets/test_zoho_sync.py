from __future__ import annotations

from typing import Any

import pytest

from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget, is_zoho_enabled


class FakeZohoClient:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self.calls: list[str] = []

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        self.calls.append("get_record")
        return self.records.get(module, {}).get(record_id)

    def insert_record(self, module: str, record: dict[str, Any]) -> str:
        self.calls.append("insert_record")
        record_id = f"zoho-{len(self.records.get(module, {})) + 1}"
        self.records.setdefault(module, {})[record_id] = dict(record)
        return record_id

    def update_record(self, module: str, record_id: str, record: dict[str, Any]) -> None:
        self.calls.append("update_record")
        self.records.setdefault(module, {}).setdefault(record_id, {}).update(record)


# --- is_zoho_enabled -------------------------------------------------------------------


def test_is_zoho_enabled_defaults_to_true_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_ZOHO", raising=False)

    assert is_zoho_enabled() is True


@pytest.mark.parametrize("raw_value", ["False", "false", "0", "no", ""])
def test_is_zoho_enabled_false_values(monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", raw_value)

    assert is_zoho_enabled() is False


@pytest.mark.parametrize("raw_value", ["True", "true", "1", "yes"])
def test_is_zoho_enabled_true_values(monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", raw_value)

    assert is_zoho_enabled() is True


# --- ZohoSyncTarget: 有効時は通常どおりクライアントを呼び出す ---------------------------


def test_enabled_target_performs_normal_crud() -> None:
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=True)

    # Notionのプロパティ名「案件名」はZohoのapi_name「Deal_Name」へ置き換えて送る
    # （2026-08-31まで置き換えておらず、Zoho側に一切書けていなかった）。
    record_id = target.upsert_record(None, {"案件名": "新規案件"}, db_key="project")
    assert client.records["案件"][record_id] == {"Deal_Name": "新規案件"}

    target.upsert_record(record_id, {"案件名": "更新後"}, db_key="project")
    assert client.records["案件"][record_id]["Deal_Name"] == "更新後"

    assert target.get_record(record_id) == {"Deal_Name": "更新後"}

    target.delete_record(record_id)
    assert client.records["案件"][record_id]["削除フラグ"] is True


def test_upsert_record_skips_when_zoho_field_cannot_be_determined() -> None:
    """送り先のapi_nameが決まらない項目は送らない。

    以前はNotionのプロパティ名をそのままZohoへ渡していたため、Zoho側では
    「知らない項目」として無視され、書けていないのに成功として数えられていた。
    """
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=True)

    assert target.upsert_record(None, {"存在しない項目": "x"}, db_key="project") is None
    assert target.upsert_record("zoho-1", {"存在しない項目": "x"}, db_key="project") == "zoho-1"
    assert client.records == {}


def test_upsert_record_skips_when_db_key_is_missing() -> None:
    """db_keyが分からなければ変換表を引けないので、素通しせず書き込まない。"""
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=True)

    assert target.upsert_record(None, {"案件名": "新規案件"}) is None
    assert client.records == {}


# --- ENABLE_ZOHO=False: クライアントを一切呼び出さずスキップする ------------------------


def test_disabled_target_skips_get_record_without_calling_client() -> None:
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=False)

    assert target.get_record("zoho-1") is None
    assert client.calls == []


def test_disabled_target_skips_upsert_record_without_calling_client() -> None:
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=False)

    result = target.upsert_record("zoho-1", {"案件名": "新規案件"})

    assert result == "zoho-1"
    assert client.calls == []
    assert client.records == {}


def test_disabled_target_upsert_record_returns_none_for_new_record() -> None:
    """WARN対応: 新規作成（external_id=None）時、無効化中は""ではなくNoneを返す
    （「作成されていない」ことを採番済みIDと誤認しないよう型で表現する）。"""
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=False)

    result = target.upsert_record(None, {"案件名": "新規案件"})

    assert result is None
    assert client.calls == []
    assert client.records == {}


def test_disabled_target_skips_delete_record_without_calling_client() -> None:
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件", enabled=False)

    target.delete_record("zoho-1")

    assert client.calls == []


def test_target_follows_env_var_when_enabled_not_explicitly_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeZohoClient()
    target = ZohoSyncTarget(client, "案件")

    monkeypatch.setenv("ENABLE_ZOHO", "False")
    assert target.get_record("zoho-1") is None
    assert client.calls == []

    monkeypatch.setenv("ENABLE_ZOHO", "True")
    target.get_record("zoho-1")
    assert client.calls == ["get_record"]


def test_get_record_logs_identifiers_and_reraises_on_client_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2026-08-27本番障害対応: kintone_sync.KintoneSyncTarget.get_record()と同じ理由で、
    例外自体は握りつぶさず呼び出し元へ伝播させ、切り分けに必要なmodule/external_id/db_key
    のみをログへ残す。"""

    class RaisingZohoClient:
        def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
            raise RuntimeError("boom")

        def insert_record(self, module: str, record: dict[str, Any]) -> str:
            raise NotImplementedError

        def update_record(self, module: str, record_id: str, record: dict[str, Any]) -> None:
            raise NotImplementedError

    target = ZohoSyncTarget(RaisingZohoClient(), "案件", enabled=True)

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            target.get_record("zoho-1", db_key="project")

    assert "案件" in caplog.text
    assert "zoho-1" in caplog.text
    assert "project" in caplog.text
