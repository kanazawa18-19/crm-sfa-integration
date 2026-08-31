"""行の新規作成をレコード単位で排他する仕組みの検証（2026-08-31）。

「同期キーで探す → 無ければ追記する」の間に別のワーカーが同じレコードの行を作ると
2行できる。同期キーで引き直す仕組みで窓は狭まったが、探すと追記の間そのものは残るため、
**行を作る瞬間だけ**advisory lockを取る（Gemini・ChatGPTの両方から指摘）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.sync_engine import spreadsheet_row_lock
from src.sync_engine.spreadsheet_row_lock import acquire_row_creation_lock, lock_key


@pytest.fixture(autouse=True)
def 警告をリセットする() -> None:
    spreadsheet_row_lock.reset_missing_database_url_warning()


def test_レコードごとに違うキーになる() -> None:
    assert lock_key("client_master", "CLI-001") != lock_key("client_master", "CLI-002")
    # db_keyも混ぜる（DBが違えば別レコード）。
    assert lock_key("client_master", "CLI-001") != lock_key("project", "CLI-001")


def test_キーは同じ入力に対して安定している() -> None:
    """プロセスをまたいで同じ値でなければ、そもそも排他にならない。"""
    assert lock_key("client_master", "CLI-001") == lock_key("client_master", "CLI-001")


def test_キーはpostgresのbigintに収まる() -> None:
    """`pg_try_advisory_lock`はint8を取る。範囲外だとエラーになる。"""
    for notion_key in ("CLI-001", "MSA-PJ-99999", "SA-AC-1"):
        key = lock_key("client_master", notion_key)
        assert -(2**63) <= key < 2**63


def test_DBが未設定なら排他せずに続行する(monkeypatch: pytest.MonkeyPatch) -> None:
    """ローカルやテストではDBが無い。ここで止めると開発が回らない。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_UNPOOLED", raising=False)

    with acquire_row_creation_lock("client_master", "CLI-001") as acquired:
        assert acquired is True


def _接続を差し替える(
    monkeypatch: pytest.MonkeyPatch, 実行したSQL: list[str], *, locked: bool
) -> None:
    class _カーソル:
        def __enter__(self) -> "_カーソル":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            実行したSQL.append(sql)

        def fetchone(self) -> dict[str, Any]:
            return {"locked": locked}

    class _接続:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> _カーソル:
            return _カーソル()

        def close(self) -> None:
            self.closed = True

    接続 = _接続()
    monkeypatch.setattr(spreadsheet_row_lock.db_utils, "connect_for_advisory_lock", lambda _l: 接続)
    return 接続


def test_取得できたらunlockして接続を閉じる(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    実行したSQL: list[str] = []
    接続 = _接続を差し替える(monkeypatch, 実行したSQL, locked=True)

    with acquire_row_creation_lock("client_master", "CLI-001") as acquired:
        assert acquired is True

    assert any("pg_try_advisory_lock" in sql for sql in 実行したSQL)
    assert any("pg_advisory_unlock" in sql for sql in 実行したSQL)
    assert 接続.closed is True


def test_取得できなければunlockせずに閉じる(monkeypatch: pytest.MonkeyPatch) -> None:
    """取っていないロックを解放すると、他のワーカーのロックを外しかねない。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    実行したSQL: list[str] = []
    接続 = _接続を差し替える(monkeypatch, 実行したSQL, locked=False)

    with acquire_row_creation_lock("client_master", "CLI-001") as acquired:
        assert acquired is False

    assert not any("pg_advisory_unlock" in sql for sql in 実行したSQL)
    assert 接続.closed is True


def test_中で例外が出ても解放して閉じる(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    実行したSQL: list[str] = []
    接続 = _接続を差し替える(monkeypatch, 実行したSQL, locked=True)

    with pytest.raises(RuntimeError):
        with acquire_row_creation_lock("client_master", "CLI-001"):
            raise RuntimeError("追記に失敗")

    assert any("pg_advisory_unlock" in sql for sql in 実行したSQL)
    assert 接続.closed is True
