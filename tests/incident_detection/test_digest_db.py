"""通知失敗時に送信済み更新へ進まないことを接続ダミーで検証する。"""
from unittest.mock import MagicMock

import pytest

from src.incident_detection import db


@pytest.mark.parametrize("failed", [False, True])
def test_digest_transaction_updates_only_after_success(monkeypatch, failed):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [{"id": "one"}, {"id": "two"}]
    monkeypatch.setattr(db, "_connect", lambda: conn)
    error = RuntimeError("送信失敗")
    try:
        with db.claim_undigested_medium_priority_emails() as rows:
            assert len(rows) == 2
            assert cur.execute.call_count == 1
            conn.commit.assert_not_called()
            if failed:
                raise error
    except RuntimeError:
        assert failed
    sql, params = cur.execute.call_args_list[0].args
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params == (50,)
    if failed:
        assert cur.execute.call_count == 1
        conn.commit.assert_not_called()
        assert conn.__exit__.call_args.args[1] is error
    else:
        assert cur.execute.call_count == 2
        assert cur.execute.call_args.args[1] == (["one", "two"],)
        conn.commit.assert_called_once()
