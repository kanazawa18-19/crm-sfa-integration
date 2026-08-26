"""src/db_utils.py（複数モジュール共有のDBまわりヘルパー）の検証。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import db_utils


@pytest.mark.parametrize(
    ("raw_microsecond", "expected_microsecond"),
    [
        (0, 0),
        (1, 0),
        (999, 0),
        (1_000, 1_000),
        (927_020, 927_000),
        (927_999, 927_000),
        (999_999, 999_000),
    ],
)
def test_db_truncated_utcnow_rounds_microsecond_down_to_multiple_of_1000(
    monkeypatch: pytest.MonkeyPatch, raw_microsecond: int, expected_microsecond: int
) -> None:
    """マイクロ秒精度の値を1000の倍数(ミリ秒境界)へ切り捨てることを確認する。1000の倍数は
    Postgresの`TIMESTAMP(3)`保存時の四捨五入(境界によっては繰り上がる)を適用しても値が
    変わらない不動点になるため、この切り捨てが誤削除防止の対策として機能する
    (`927_999`のような、素の値なら繰り上がりうる境界値も含めて検証する)。"""
    fixed_now = datetime(2026, 8, 25, 12, 0, 0, raw_microsecond, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return fixed_now

    monkeypatch.setattr(db_utils, "datetime", _FixedDatetime)

    result = db_utils.db_truncated_utcnow()

    assert result.microsecond == expected_microsecond
    assert result.tzinfo == timezone.utc
    assert result.replace(microsecond=raw_microsecond) == fixed_now


def test_ensure_utc_attaches_utc_tzinfo_to_naive_datetime() -> None:
    """psycopg経由で読んだ`TIMESTAMP(3)`列由来のtz-naiveなdatetimeを、UTCとして
    tz-awareに変換することを確認する(2026-08-26、gmail_sync watch_registrationの
    本番クラッシュの再発防止用ヘルパー)。"""
    naive = datetime(2026, 8, 26, 3, 0, 0)

    result = db_utils.ensure_utc(naive)

    assert result == datetime(2026, 8, 26, 3, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo == timezone.utc


def test_ensure_utc_leaves_tz_aware_datetime_unchanged() -> None:
    aware = datetime(2026, 8, 26, 3, 0, 0, tzinfo=timezone.utc)

    assert db_utils.ensure_utc(aware) == aware
    assert db_utils.ensure_utc(aware) is aware


def test_db_truncated_utcnow_is_idempotent_under_further_truncation() -> None:
    """今回の不具合の本質(`保存後の丸め済みの値 < DELETEのWHEREに使う元の値`が真に
    なってしまう)は、基準時刻がPostgres保存時の丸め(四捨五入)を通しても変化しない
    (不動点になる)ことで解消される。1000の倍数はPostgresが切り捨てても繰り上げても
    値が変わらないため、ここでは`db_truncated_utcnow()`の戻り値をさらにミリ秒境界へ
    切り捨てる演算を適用しても、元の値と完全一致すること(＝DELETEの比較対象と保存値が
    一致し、誤って削除されないこと)を確認する。"""
    value = db_utils.db_truncated_utcnow()

    postgres_stored_value = value.replace(microsecond=(value.microsecond // 1000) * 1000)

    assert postgres_stored_value == value
    assert not (postgres_stored_value < value)
