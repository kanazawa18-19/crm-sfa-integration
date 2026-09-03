"""EmailLogの一括追記まわりの検証（2026-09-03）。

実DBには繋がず、`_connect()`をフェイクに差し替えて発行SQLと戻り値だけを見る。
`cur.rowcount`をそのまま「追記件数」としてログに出しているため、その約束を固定しておく
（psycopg3の`executemany`は`ON CONFLICT DO NOTHING`で弾かれた行を0として累積するので、
実際の挿入件数になる。実Postgres上でも確認済み）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.gmail_sync import db


class _FakeCursor:
    def __init__(self, rowcount: int = 0) -> None:
        self.rowcount = rowcount
        self.executemany_calls: list[tuple[str, list]] = []

    def executemany(self, sql: str, params: list) -> None:
        self.executemany_calls.append((sql, params))
        self.rowcount = len(params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _row(message_id: str = "m1") -> db.EmailLogRow:
    return db.EmailLogRow(
        contact_page_id="cnt-1",
        contact_email="lead@client.example.com",
        rep_email="rep@cnctor.jp",
        gmail_message_id=message_id,
        direction="inbound",
        subject="件名",
        snippet="本文の先頭",
        sent_at=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
    )


def test_insert_email_logs_does_not_touch_the_db_for_an_empty_list(monkeypatch) -> None:
    def fail_connect():
        raise AssertionError("0件のときは接続しないこと")

    monkeypatch.setattr(db, "_connect", fail_connect)

    assert db.insert_email_logs([]) == 0


def test_insert_email_logs_returns_the_row_count(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(db, "_connect", lambda: connection)

    inserted = db.insert_email_logs([_row("m1"), _row("m2")])

    assert inserted == 2
    assert connection.committed is True


def test_insert_email_logs_skips_duplicates_instead_of_raising(monkeypatch) -> None:
    """`gmailMessageId`が既にある行は例外ではなく黙って飛ばす（取り込みの流し直しが冪等）。"""
    cursor = _FakeCursor()
    monkeypatch.setattr(db, "_connect", lambda: _FakeConnection(cursor))

    db.insert_email_logs([_row("m1")])

    sql, params = cursor.executemany_calls[0]
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    assert len(params) == 1


def test_insert_email_logs_never_writes_incident_columns(monkeypatch) -> None:
    """★ 過去分にインシデントスコアを付けない。付けると日次ダイジェストへ一斉に載る。"""
    cursor = _FakeCursor()
    monkeypatch.setattr(db, "_connect", lambda: _FakeConnection(cursor))

    db.insert_email_logs([_row("m1")])

    sql, _ = cursor.executemany_calls[0]
    assert "incidentScore" not in sql
    assert "incidentPriority" not in sql


def test_insert_email_logs_passes_every_row_in_one_statement(monkeypatch) -> None:
    """1行1接続にしない（数千通を辿るとき接続コストだけで終わらなくなる）。"""
    cursor = _FakeCursor()
    monkeypatch.setattr(db, "_connect", lambda: _FakeConnection(cursor))

    db.insert_email_logs([_row(f"m{i}") for i in range(50)])

    assert len(cursor.executemany_calls) == 1
    assert len(cursor.executemany_calls[0][1]) == 50
