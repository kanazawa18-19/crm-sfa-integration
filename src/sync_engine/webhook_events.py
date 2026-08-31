"""Webhookの再送を弾く（イベントIDによる重複排除、2026-09-01）。

**Notionは配信に失敗すると最大8回、およそ24時間かけて再送する。**
購読を有効にしたことで新しく生まれたリスクで、再送のたびに同じ書き込みが走る。
値が同じなら実害は出にくいが、正しくはイベントIDで弾く。

■ どう弾くか

`INSERT ... ON CONFLICT DO NOTHING RETURNING` の1クエリで
「初めて見たか」を原子的に判定する。SELECTしてからINSERTすると、
**ほぼ同時に届いた再送の両方が「初めて」と判定される**レースが残る
（`src/incident_detection/db.py`の`claim_undigested_...`と同じ考え方）。

■ 処理に失敗したら記録を消す

先に記録してから処理すると、処理中に落ちたときに再送まで弾いてしまい、
**その変更が永久に失われる**。失敗したら記録を消して、再送で拾えるようにする。

■ 溜め続けない

イベントIDは増え続ける。`purge_old_events()`を日次バッチから呼んで古い分を消す
（ポーリング型の取り込みで「stateを無期限に蓄積しない」としたのと同じ理由）。
再送はおよそ24時間以内なので、保持は7日あれば十分すぎる。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

#: 保持日数。Notionの再送は最大でもおよそ24時間以内。
RETENTION_DAYS = 7


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def claim_event(event_id: str, source: str) -> bool:
    """このイベントを処理してよければTrue、既に処理済みならFalse。

    **判定できないとき（DB未設定・接続不可）はTrueを返す。**
    重複排除ができないことを理由に同期を止める方が害が大きい。
    """
    if not event_id:
        return True
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "WebhookEvent" (id, source, "receivedAt")
                VALUES (%s, %s, now())
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (event_id, source),
            )
            claimed = cur.fetchone() is not None
            conn.commit()
        return claimed
    except Exception:  # noqa: BLE001 (重複排除の失敗で同期を止めない)
        logger.warning(
            "claim_event: 重複判定ができませんでした。処理を続けます (event_id=%r)",
            event_id,
            exc_info=True,
        )
        return True


def release_event(event_id: str) -> None:
    """処理に失敗したイベントの記録を消し、再送で拾えるようにする。"""
    if not event_id:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM "WebhookEvent" WHERE id = %s', (event_id,))
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "release_event: 記録を消せませんでした。この再送は弾かれます (event_id=%r)",
            event_id,
            exc_info=True,
        )


def purge_old_events(days: int = RETENTION_DAYS) -> int:
    """古いイベント記録を消す。消した件数を返す（日次バッチから呼ぶ）。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'DELETE FROM "WebhookEvent" WHERE "receivedAt" < now() - make_interval(days => %s)',
            (days,),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted
