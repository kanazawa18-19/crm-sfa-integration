"""src/db_utils.py（複数モジュール共有のDBまわりヘルパー）の検証。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

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


# --- connect_for_advisory_lock ----------------------------------------------------------------
# advisory lock専用接続(2026-08-28)。PgBouncerのtransaction poolingではセッション単位の
# advisory lockが例外を出さないまま無言で機能しなくなる(Neonのpooled DATABASE_URLが本番で
# 使われていたことが発覚した問題への対処)ため、DATABASE_URL_UNPOOLEDを優先して使う。


def _capture_connect(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_connect(url: str, **kwargs: Any) -> str:
        calls.append({"url": url, "kwargs": kwargs})
        return "fake-connection"  # type: ignore[return-value]

    monkeypatch.setattr(db_utils.psycopg, "connect", _fake_connect)
    return calls


def test_connect_for_advisory_lock_uses_unpooled_url_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://user:pass@direct-host/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@pooled-host-pooler/db")
    calls = _capture_connect(monkeypatch)
    logger = logging.getLogger("test_connect_for_advisory_lock_uses_unpooled_url_when_set")

    result = db_utils.connect_for_advisory_lock(logger)

    assert result == "fake-connection"
    assert len(calls) == 1
    assert calls[0]["url"] == "postgresql://user:pass@direct-host/db"


def test_connect_for_advisory_lock_warns_when_unpooled_url_itself_looks_pooled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`DATABASE_URL_UNPOOLED`は設定されているが値そのものがpooledらしき場合(Vercelでの
    貼り付けミス、Neon側の命名規則変更等)も、フォールバック経路と同様に警告すること
    (shirokuma-secレビューWARN対応、2026-08-28)。フォールバック時の警告と文面が
    区別できることも確認する。"""
    monkeypatch.setenv(
        "DATABASE_URL_UNPOOLED", "postgresql://user:pass@ep-example-pooler.aws.neon.tech/db"
    )
    calls = _capture_connect(monkeypatch)
    logger = logging.getLogger(
        "test_connect_for_advisory_lock_warns_when_unpooled_url_itself_looks_pooled"
    )

    with caplog.at_level(logging.WARNING, logger=logger.name):
        db_utils.connect_for_advisory_lock(logger)

    assert calls[0]["url"] == "postgresql://user:pass@ep-example-pooler.aws.neon.tech/db"
    messages = [record.getMessage() for record in caplog.records]
    assert any("DATABASE_URL_UNPOOLED" in m for m in messages)
    # フォールバックしたわけではないので、その旨の文言は出ないこと。
    assert not any("falls back to" in m for m in messages)


def test_connect_for_advisory_lock_falls_back_to_database_url_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DATABASE_URL_UNPOOLED未設定時はDATABASE_URLへフォールバックするが、無言で今回の
    問題に戻らないよう必ずwarningログを出すこと。"""
    monkeypatch.delenv("DATABASE_URL_UNPOOLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@direct-host/db")
    calls = _capture_connect(monkeypatch)
    logger = logging.getLogger("test_connect_for_advisory_lock_falls_back_to_database_url_and_warns")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        db_utils.connect_for_advisory_lock(logger)

    assert calls[0]["url"] == "postgresql://user:pass@direct-host/db"
    messages = [record.getMessage() for record in caplog.records]
    assert any("DATABASE_URL_UNPOOLED" in m for m in messages)
    # ホスト名に"-pooler"を含まないため、より強い(pooled接続らしき)警告までは出ない。
    assert not any("mutual exclusion" in m for m in messages)


def test_connect_for_advisory_lock_warns_more_strongly_when_fallback_is_pooled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """フォールバック先のホスト名に'-pooler'が含まれる場合、advisory lockが機能しない
    可能性が高いことをより強く警告すること。"""
    monkeypatch.delenv("DATABASE_URL_UNPOOLED", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:pass@ep-example-pooler.aws.neon.tech/db"
    )
    _capture_connect(monkeypatch)
    logger = logging.getLogger(
        "test_connect_for_advisory_lock_warns_more_strongly_when_fallback_is_pooled"
    )

    with caplog.at_level(logging.WARNING, logger=logger.name):
        db_utils.connect_for_advisory_lock(logger)

    messages = [record.getMessage() for record in caplog.records]
    assert any("DATABASE_URL_UNPOOLED" in m for m in messages)
    assert any("mutual exclusion" in m for m in messages)


def test_connect_for_advisory_lock_raises_when_neither_url_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL_UNPOOLED", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    logger = logging.getLogger("test_connect_for_advisory_lock_raises_when_neither_url_is_set")

    with pytest.raises(ValueError, match="DATABASE_URL_UNPOOLED"):
        db_utils.connect_for_advisory_lock(logger)


def test_connect_for_advisory_lock_never_logs_connection_string_or_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """フォールバック時の警告ログに、接続文字列そのもの(ホスト名・パスワード)が含まれない
    こと(`-pooler`を含むかどうかの真偽値のみ判定に使い、ログメッセージには埋め込まない)。"""
    monkeypatch.delenv("DATABASE_URL_UNPOOLED", raising=False)
    secret_password = "sup3r-secret-p4ssw0rd"
    pooled_host = "ep-example-pooler.aws.neon.tech"
    monkeypatch.setenv("DATABASE_URL", f"postgresql://user:{secret_password}@{pooled_host}/db")
    _capture_connect(monkeypatch)
    logger = logging.getLogger(
        "test_connect_for_advisory_lock_never_logs_connection_string_or_password"
    )

    with caplog.at_level(logging.WARNING, logger=logger.name):
        db_utils.connect_for_advisory_lock(logger)

    full_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_password not in full_log_text
    assert pooled_host not in full_log_text
