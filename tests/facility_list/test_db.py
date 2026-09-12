"""src/facility_list/infrastructure/db.py の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替えて、
発行されるSQL・パラメータを検証する（tests/project_mirror/test_db.py と同じパターン）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.facility_list.domain.models import (
    CheckInMachineSource,
    CheckInMachineStatus,
    CustomPageStatus,
    Facility,
    FacilityCategory,
)
from src.facility_list.infrastructure import db


class _FakeCursor:
    def __init__(
        self,
        fetch_rows: list[dict[str, Any]] | None = None,
        fetch_one_rows: list[dict[str, Any] | None] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.executed_many: list[tuple[str, list[Any]]] = []
        self._fetch_rows = fetch_rows or []
        self._fetch_one_rows = list(fetch_one_rows or [])
        self.rowcount = rowcount

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def executemany(self, sql: str, params: list[Any]) -> None:
        self.executed_many.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._fetch_rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._fetch_one_rows.pop(0) if self._fetch_one_rows else None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _set_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")


def _patch_connect(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> _FakeConnection:
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: conn)
    return conn


def _facility(**overrides) -> Facility:
    base = {
        "hotel_no": 1234,
        "name": "テスト旅館",
        "prefecture": "鳥取県",
        "city": "米子市",
        "room_count": 50,
        "review_average": Decimal("4.2"),
        "min_charge": 8000,
        "category": FacilityCategory.RYOKAN,
        "custom_page_status": CustomPageStatus.NOT_PUBLISHED,
        "facilities": ("大浴場",),
    }
    base.update(overrides)
    return Facility(**base)


def test_DATABASE_URLが無ければ接続しない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        db._connect()


class TestUpsertFacilities:
    def test_列とプレースホルダの数が一致する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 列を足したときにVALUES側の%sを増やし忘れると本番で初めて落ちる。
        cursor = _FakeCursor()
        _patch_connect(monkeypatch, cursor)
        db.upsert_facilities([_facility()])

        sql, rows = cursor.executed_many[0]
        columns_part = sql.split("INSERT INTO")[1].split("VALUES")[0]
        values_part = sql.split("VALUES")[1].split("ON CONFLICT")[0]
        column_count = columns_part.count(",") + 1
        placeholder_count = values_part.count("%s")
        assert column_count == placeholder_count, f"列{column_count}個 vs %s{placeholder_count}個"
        assert len(rows[0]) == placeholder_count

    def test_enumは値で渡しキャストする(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor()
        _patch_connect(monkeypatch, cursor)
        db.upsert_facilities([_facility()])
        sql, rows = cursor.executed_many[0]
        assert '%s::"FacilityCategory"' in sql
        assert '%s::"CustomPageStatus"' in sql
        assert "ryokan" in rows[0]
        assert "not_published" in rows[0]

    def test_hotelNoで上書きする(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor()
        _patch_connect(monkeypatch, cursor)
        db.upsert_facilities([_facility()])
        sql, _ = cursor.executed_many[0]
        assert 'ON CONFLICT ("hotelNo") DO UPDATE' in sql
        # 取り込み直しでチェックイン機の判定(別テーブル)を消さないこと。
        assert "FacilityCheckInMachine" not in sql

    def test_空なら何もしない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor()
        _patch_connect(monkeypatch, cursor)
        assert db.upsert_facilities([]) == 0
        assert cursor.executed_many == []

    def test_commitする(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor()
        conn = _patch_connect(monkeypatch, cursor)
        db.upsert_facilities([_facility()])
        assert conn.committed is True


class TestMarkUnlisted:
    def test_行を消さずフラグだけ落とす(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ProjectMirror全消失インシデントの教訓。母集団は消すより残す。
        cursor = _FakeCursor(rowcount=2)
        _patch_connect(monkeypatch, cursor)
        assert db.mark_unlisted([1, 2]) == 2
        sql, params = cursor.executed[0]
        assert "UPDATE" in sql and "DELETE" not in sql.upper()
        assert '"isListed" = false' in sql
        assert params[1] == [1, 2]

    def test_空なら何もしない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor()
        _patch_connect(monkeypatch, cursor)
        assert db.mark_unlisted([]) == 0
        assert cursor.executed == []


class TestUpsertCheckInMachine:
    def test_根拠つきで保存する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor()
        _patch_connect(monkeypatch, cursor)
        db.upsert_check_in_machine(
            1234,
            status=CheckInMachineStatus.YES,
            source=CheckInMachineSource.WEB_SEARCH,
            evidence="公式サイトに記載",
            evidence_url="https://example.com",
        )
        sql, params = cursor.executed[0]
        assert '%s::"CheckInMachineStatus"' in sql
        assert params[:5] == (1234, "yes", "web_search", "公式サイトに記載", "https://example.com")


class TestFetchFacilities:
    def test_都道府県で絞りチェックイン機を結合する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor(fetch_rows=[])
        _patch_connect(monkeypatch, cursor)
        db.fetch_facilities(prefectures=["鳥取県", "島根県"])
        sql, params = cursor.executed[0]
        assert 'LEFT JOIN "FacilityCheckInMachine"' in sql
        assert '"isListed" = true' in sql
        assert params[0] == ["鳥取県", "島根県"]

    def test_チェックイン機が未登録なら不明として読む(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = {
            "hotelNo": 1,
            "name": "テスト",
            "address": None,
            "postalCode": None,
            "prefecture": "鳥取県",
            "city": None,
            "roomCount": 30,
            "reviewAverage": Decimal("4.0"),
            "reviewCount": 10,
            "photoCount": 5,
            "minCharge": 7000,
            "category": "ryokan",
            "categoryConfidence": "medium",
            "hasOnsen": True,
            "customPageStatus": "not_published",
            "customPageCount": 0,
            "facilities": ["大浴場"],
            "roomFacilities": None,
            "isListed": True,
            "checkInStatus": None,  # LEFT JOINで行が無いときのNone
            "checkInSource": None,
        }
        cursor = _FakeCursor(fetch_rows=[row])
        _patch_connect(monkeypatch, cursor)
        facilities = db.fetch_facilities()
        assert len(facilities) == 1
        facility = facilities[0]
        assert facility.check_in_machine is CheckInMachineStatus.UNKNOWN
        assert facility.check_in_machine_source is CheckInMachineSource.NONE
        assert facility.min_charge == 7000
        assert facility.facilities == ("大浴場",)
        assert facility.room_facilities == ()

    def test_件数の集計SQLが壊れていない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeCursor(
            fetch_one_rows=[{"total": 10, "listed": 9, "missing_room_count": 1, "with_warning": 0}]
        )
        _patch_connect(monkeypatch, cursor)
        counts = db.count_facilities()
        assert counts == {"total": 10, "listed": 9, "missing_room_count": 1, "with_warning": 0}


class TestClientNameIndexHealth:
    """CRMミラーが空・古いときに新規リストを作らせないための確認。"""

    def test_件数と同期実行時刻を読む(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime, timezone

        changed = datetime(2026, 9, 7, 19, 1, tzinfo=timezone.utc)
        ran = datetime(2026, 9, 11, 19, 4, tzinfo=timezone.utc)
        cursor = _FakeCursor(
            fetch_one_rows=[{"n": 102813, "changed": changed}, {"updatedAt": ran}]
        )
        _patch_connect(monkeypatch, cursor)
        health = db.client_name_index_health()
        assert health.row_count == 102813
        assert health.last_run_at == ran
        assert health.last_changed_at == changed
        assert "ClientNameIndex" in cursor.executed[0][0]
        assert "SyncCursor" in cursor.executed[1][0]

    def test_中身が変わっていなくても同期が走っていれば使える(self) -> None:
        # 取引先に変更が無ければ1行も更新されないので、`syncedAt`は古いままになる。
        # それを「同期が止まっている」と読むと、正常なのに新規リストが作れなくなる。
        # **本番で実際に起きていた**(2026-09-12、配備前の確認で発見)。
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        health = db.ClientNameIndexHealth(
            row_count=102813,
            last_run_at=now - timedelta(hours=12),
            last_changed_at=now - timedelta(days=5),
        )
        assert health.is_fresh() is True

    def test_空なら使えないと判定する(self) -> None:
        from datetime import datetime, timezone

        health = db.ClientNameIndexHealth(row_count=0, last_run_at=datetime.now(timezone.utc))
        assert health.is_fresh() is False

    def test_同期が止まっていれば使えないと判定する(self) -> None:
        from datetime import datetime, timedelta, timezone

        stale = datetime.now(timezone.utc) - timedelta(days=3)
        health = db.ClientNameIndexHealth(row_count=102813, last_run_at=stale)
        assert health.is_fresh() is False

    def test_一度も走っていなければ使えない(self) -> None:
        health = db.ClientNameIndexHealth(row_count=102813, last_run_at=None)
        assert health.is_fresh() is False

    def test_新しくて件数が十分なら使える(self) -> None:
        from datetime import datetime, timezone

        health = db.ClientNameIndexHealth(
            row_count=102813, last_run_at=datetime.now(timezone.utc)
        )
        assert health.is_fresh() is True
