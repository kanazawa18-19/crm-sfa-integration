"""配信停止の読み取り（`fetch_opt_outs`）の検証（2026-09-03）。

実DBには繋がず、`_connect()`をフェイクに差し替えて**発行SQLと渡すパラメータ**を見る
（`tests/gmail_sync/test_db.py`と同じ方式）。

ここは一斉配信で一番壊してはいけない箇所。除外漏れは「止めてと言った相手に営業メールを
送る」＝特定電子メール法違反になるため、SQLの条件そのものを固定しておく。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.bulk_email import db

# Notionから来る形（ハイフン付き）と、DBに保存されている形（正規化済み）。
PAGE_ID = "3ced8ea8-1234-814a-83ce-cb3645539acd"
NORMALIZED = "3ced8ea81234814a83cecb3645539acd"


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> _FakeCursor:
    cursor = _FakeCursor(rows)
    monkeypatch.setattr(db, "_connect", lambda: _FakeConnection(cursor))
    return cursor


def test_停止中の行だけを見るSQLを発行する(monkeypatch: pytest.MonkeyPatch) -> None:
    """`unsubscribed = TRUE`の条件が落ちると、停止を解除した相手まで除外してしまう。
    逆に条件を反転させると、止めた相手に送ってしまう。"""
    cursor = _patch(monkeypatch, [])
    db.fetch_opt_outs([PAGE_ID], ["a@example.com"])

    sql, params = cursor.executed[0]
    assert '"unsubscribed" = TRUE' in sql
    assert 'lower("contactEmail") = ANY(%s)' in sql
    assert params == ([NORMALIZED], ["a@example.com"])


def test_問い合わせは候補だけに絞る(monkeypatch: pytest.MonkeyPatch) -> None:
    """テーブル全件読みにすると、行数が伸びても遅くなるだけで誰も気づけない。"""
    cursor = _patch(monkeypatch, [])
    db.fetch_opt_outs([PAGE_ID, "other-id"], [])

    _, params = cursor.executed[0]
    assert sorted(params[0]) == sorted([NORMALIZED, "otherid"])


def test_アドレスは小文字に揃えて渡す(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _patch(monkeypatch, [])
    db.fetch_opt_outs([], [" A@Example.COM ", "a@example.com"])

    _, params = cursor.executed[0]
    assert params[1] == ["a@example.com"]


def test_ページIDは呼び出し元が持っている元の形で返す(monkeypatch: pytest.MonkeyPatch) -> None:
    """DBは正規化済み（ハイフン無し）で持っているが、除外判定はNotion由来の形で行う。
    ここで形が食い違うと、停止済みの相手が「停止していない」ものとして通ってしまう。"""
    _patch(monkeypatch, [{"contactPageId": NORMALIZED, "contactEmail": "A@Example.com"}])

    ids, emails = db.fetch_opt_outs([PAGE_ID], [])

    assert ids == {PAGE_ID}
    assert emails == {"a@example.com"}


def test_候補に無いページIDは返さない(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [{"contactPageId": "ffffffffffffffffffffffffffffffff", "contactEmail": ""}])
    ids, emails = db.fetch_opt_outs([PAGE_ID], [])
    assert ids == set()
    assert emails == set()


def test_候補が空ならDBに触らない(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _patch(monkeypatch, [])
    assert db.fetch_opt_outs([" "], [""]) == (set(), set())
    assert cursor.executed == []


def test_読み取りに失敗したら例外をそのまま上げる(monkeypatch: pytest.MonkeyPatch) -> None:
    """空集合を返すと「配信停止が0人」として扱われ、止めた相手に送ってしまう。"""

    def _boom() -> None:
        raise RuntimeError("DBに繋がらない")

    monkeypatch.setattr(db, "_connect", _boom)
    with pytest.raises(RuntimeError):
        db.fetch_opt_outs([PAGE_ID], [])
