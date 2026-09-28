"""共有接続で起動パラメータを使わず、取引内の期限を設定する。"""
from contextlib import contextmanager
import pytest
from src.sync_engine.won_product_link_queue import ProjectProductLinkQueue


def test_local_timeout_is_set_before_queries_and_error_closes(monkeypatch):
    calls = []

    class Connection:
        def execute(self, sql):
            calls.append(sql)

    @contextmanager
    def connect(url, **kwargs):
        assert "options" not in kwargs
        calls.append("open")
        try:
            yield Connection()
        except ValueError:
            calls.append("rollback")
            raise
        finally:
            calls.append("close")

    monkeypatch.setenv("DATABASE_URL", "postgresql://local-test")
    monkeypatch.setattr("src.sync_engine.won_product_link_queue.psycopg.connect", connect)
    with pytest.raises(ValueError):
        with ProjectProductLinkQueue()._connect() as conn:
            conn.execute("SELECT 1")
            raise ValueError("試験")
    assert calls == ["open", "SET LOCAL statement_timeout = '10s'", "SELECT 1", "rollback", "close"]
