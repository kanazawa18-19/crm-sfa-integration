from __future__ import annotations

from typing import Any

import pytest

from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget


class FakeKintoneClient:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self._next_id: dict[str, int] = {}

    def get_record(self, app: str, record_id: str) -> dict[str, Any] | None:
        return self.records.get(app, {}).get(record_id)

    def add_record(self, app: str, record: dict[str, Any]) -> str:
        record_id = str(self._next_id.get(app, 0) + 1)
        self._next_id[app] = int(record_id)
        self.records.setdefault(app, {})[record_id] = dict(record)
        return record_id

    def update_record(self, app: str, record_id: str, record: dict[str, Any]) -> None:
        self.records.setdefault(app, {}).setdefault(record_id, {}).update(record)


def test_get_record_delegates_to_get_record_scoped_to_app() -> None:
    client = FakeKintoneClient()
    client.records["取引先マスタ"] = {"1001": {"取引先名": "テスト商店"}}
    target = KintoneSyncTarget(client, "取引先マスタ")

    assert target.get_record("1001") == {"取引先名": "テスト商店"}
    assert target.get_record("9999") is None


def test_upsert_record_adds_when_external_id_none() -> None:
    client = FakeKintoneClient()
    target = KintoneSyncTarget(client, "取引先マスタ")

    # Notionのプロパティ名「取引先名」はkintoneのフィールドコード「顧客名」へ置き換える
    # （kintoneのフィールドコードは画面のラベルと別物）。
    record_id = target.upsert_record(None, {"取引先名": "新規取引先"}, db_key="client_master")

    assert client.records["取引先マスタ"][record_id] == {"顧客名": "新規取引先"}


def test_upsert_record_updates_existing_record() -> None:
    client = FakeKintoneClient()
    client.records["取引先マスタ"] = {"1001": {"顧客名": "旧名称"}}
    target = KintoneSyncTarget(client, "取引先マスタ")

    result = target.upsert_record("1001", {"取引先名": "新名称"}, db_key="client_master")

    assert result == "1001"
    assert client.records["取引先マスタ"]["1001"] == {"顧客名": "新名称"}


def test_upsert_record_skips_when_field_code_cannot_be_determined() -> None:
    """フィールドコードが決まらない項目は送らない（レコードは作らない）。"""
    client = FakeKintoneClient()
    target = KintoneSyncTarget(client, "取引先マスタ")

    assert target.upsert_record(None, {"存在しない項目": "x"}, db_key="client_master") is None
    assert client.records == {}


def test_delete_record_sets_delete_flag_instead_of_removing_record() -> None:
    client = FakeKintoneClient()
    client.records["取引先マスタ"] = {"1001": {"取引先名": "テスト商店"}}
    target = KintoneSyncTarget(client, "取引先マスタ")

    target.delete_record("1001")

    record = client.records["取引先マスタ"]["1001"]
    assert record["削除フラグ"] is True
    assert record["取引先名"] == "テスト商店"


def test_get_record_logs_identifiers_and_reraises_on_client_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2026-08-27本番障害対応: get_record()自体は例外を握りつぶさず呼び出し元へ伝播させる
    （握るかどうかの判断はDispatcher側の責務）。切り分けに必要なapp/external_id/db_keyを
    ログへ残すことのみ本クラスの責務とする。"""

    class RaisingKintoneClient:
        def get_record(self, app: str, record_id: str) -> dict[str, Any] | None:
            raise RuntimeError("HTTP 400: 不正なリクエストです。")

        def add_record(self, app: str, record: dict[str, Any]) -> str:
            raise NotImplementedError

        def update_record(self, app: str, record_id: str, record: dict[str, Any]) -> None:
            raise NotImplementedError

    target = KintoneSyncTarget(RaisingKintoneClient(), "取引先マスタ")

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            target.get_record("1001", db_key="client_master")

    assert "取引先マスタ" in caplog.text
    assert "1001" in caplog.text
    assert "client_master" in caplog.text
