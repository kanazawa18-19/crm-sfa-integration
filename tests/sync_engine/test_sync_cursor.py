"""しおり（`SyncCursor`）の基準時刻がミリ秒境界に乗っていること（2026-09-01）。

■ なぜこのテストが要るのか

`pass_started_at` は、この一巡で取り込む行の `syncedAt`（`TIMESTAMP(3)`）として
**書き込みにも**使い、一巡し終えたあとの掃除の `WHERE "syncedAt" < 基準時刻` という
**比較にも**使う。素の `datetime.now(timezone.utc)`（マイクロ秒精度）だと、
書き込み時はPostgresが四捨五入でミリ秒へ丸める一方、比較には丸められていない元の値が
使われるため、丸め方向次第で **今まさに書き込んだ行まで掃除で消える。**

2026-08-25に本番で実際に起きた事故そのもの（ProjectMirror全消失）。
分割実行を新設したときに、基準時刻だけこの保護（`db_truncated_utcnow()`）を
通っていなかった。動物チーム3体とも同じ箇所を独立に指摘した。

一度DBへ保存して読み直せば `TIMESTAMP(3)` 列を経由するので結果的に丸まるが、
**一巡が1回の実行で完結する場合はDBを経由しない**ため、そこが穴だった。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.sync_engine import sync_cursor as cursor_module


def _postgres_timestamp3(value: datetime) -> datetime:
    """Postgresの`TIMESTAMP(3)`の丸め（四捨五入）を再現する。

    実DBで確認済み（2026-09-01）:
        '...123456' → '...123000'（切り捨て方向）
        '...123654' → '...124000'（繰り上げ方向）
    """
    micro = value.microsecond
    rounded_ms = (micro + 500) // 1000
    if rounded_ms >= 1000:
        return value.replace(microsecond=0) + timedelta(seconds=1)
    return value.replace(microsecond=rounded_ms * 1000)


def test_new_pass_starts_on_a_millisecond_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """しおりが無いとき（新しい一巡）の基準時刻はミリ秒境界に乗っていること。"""
    monkeypatch.setattr(cursor_module, "_connect", _no_row_connection)

    cursor = cursor_module.load_cursor("project_mirror")

    assert cursor.watermark is None
    assert cursor.pass_started_at.microsecond % 1000 == 0, (
        "基準時刻がミリ秒境界に乗っていない。TIMESTAMP(3)の丸めで"
        "書き込んだばかりの行が掃除で消える（2026-08-25の事故と同じ形）"
    )


def test_the_boundary_value_survives_the_postgres_rounding_unchanged() -> None:
    """ミリ秒境界の値は、Postgresの丸めを通しても変わらない（不動点）。

    これが成り立つ限り「保存値 < 比較用の元の値」は絶対に真にならず、
    書き込んだばかりの行が掃除で消えることはない。
    """
    from src.db_utils import db_truncated_utcnow

    for _ in range(200):
        base = db_truncated_utcnow()
        assert _postgres_timestamp3(base) == base


def test_a_raw_microsecond_value_would_have_been_deleted() -> None:
    """**素の`datetime.now()`だと実際に誤削除が起きる**ことを示す（回帰の意味づけ）。

    このテストが落ちるようになったら、それは丸めの前提が変わったということ。
    """
    unsafe = datetime(2026, 9, 1, 12, 0, 0, 123_456, tzinfo=timezone.utc)
    stored = _postgres_timestamp3(unsafe)

    # 掃除は `DELETE WHERE "syncedAt" < 基準時刻`。保存値の方が小さいと消える。
    assert stored < unsafe, "この前提が崩れたなら、丸めの挙動が変わっている"

    safe = unsafe.replace(microsecond=(unsafe.microsecond // 1000) * 1000)
    assert not (_postgres_timestamp3(safe) < safe), "ミリ秒境界なら消えない"


class _FakeCursor:
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, *args: object, **kwargs: object) -> None:
        return None

    def fetchone(self) -> None:
        return None


class _FakeConnection:
    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()


def _no_row_connection() -> _FakeConnection:
    """`SyncCursor`に行が無い状態（＝新しい一巡）を再現する。"""
    return _FakeConnection()
