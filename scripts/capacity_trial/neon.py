"""専用NeonのSELECTとセッション排他を確認する。業務同期の測定ではない。"""
from contextlib import ExitStack
import time

from .guard import NEON_PROJECT, NEON_BRANCH, NEON_ENDPOINT, validate_environment, validate_neon


def probe(dsn, ledger):
    validate_environment()
    values = validate_neon(dsn)
    ledger.reserve(sql_connections=2, sql_statements=12)
    import psycopg
    events = []
    sql_count = 0
    # PostgreSQLの同じセッションで保持・解放されるかを2接続で確認する。
    key = 260910314
    with ExitStack() as stack:
        connections = []
        for index in range(2):
            start = time.monotonic()
            connection = stack.enter_context(psycopg.connect(**values, autocommit=True))
            connections.append(connection)
            events.append({"connection": index+1, "event": "opened", "seconds": time.monotonic()-start})
        first, second = connections
        def query(connection, sql, params=None):
            nonlocal sql_count
            start = time.monotonic()
            row = connection.execute(sql, params).fetchone()
            sql_count += 1
            events.append({"connection": connections.index(connection)+1,
                           "event": "query_finished", "seconds": time.monotonic()-start})
            return row
        identity = [query(c, "SELECT current_database(), current_user, pg_backend_pid()") for c in connections]
        if any(row[:2] != ("neondb", "neondb_owner") for row in identity) or identity[0][2] == identity[1][2]:
            raise AssertionError("専用DB/role/独立sessionの照合失敗")
        if query(first, "SELECT pg_try_advisory_lock(%s)", (key,)) != (True,):
            raise AssertionError("初回ロック取得失敗")
        if query(second, "SELECT pg_try_advisory_lock(%s)", (key,)) != (False,):
            raise AssertionError("別sessionが同じロックを取得した")
        if query(first, "SELECT pg_advisory_unlock(%s)", (key,)) != (True,):
            raise AssertionError("所有sessionでのロック解放失敗")
        if query(second, "SELECT pg_try_advisory_lock(%s)", (key,)) != (True,):
            raise AssertionError("解放後の別session取得失敗")
        if query(second, "SELECT pg_advisory_unlock(%s)", (key,)) != (True,):
            raise AssertionError("別sessionの終了時解放失敗")
    connections_closed = all(connection.closed for connection in connections)
    if not connections_closed:
        raise AssertionError("終了後もSQL接続が残っています")
    return {"project": NEON_PROJECT, "branch": NEON_BRANCH, "endpoint": NEON_ENDPOINT,
            "connection_count": len(connections), "sql_count": sql_count, "connections_closed": connections_closed,
            "advisory_exclusion": True, "events": events,
            "limitations": "専用SQLプローブ。既存同期の接続ピーク・outbox・外部連携は未検証"}
