"""連絡先ごとの返信傾向（返信ラグ・返ってきやすい時間帯）を画面向けのdictにする
（2026-09-03）。

`src/analytics/reply_timing.py`（純粋関数）と`src/gmail_sync/db.py`（EmailLogの読み取り）
を繋ぐだけの薄い層。顧客360度ビュー（`client_360_service.get_client_360()`）から呼ばれ、
連絡先1件ごとにこの結果がぶら下がる。

■ 集計の単位は「アドレス」ではなく「連絡先ページ」

同じ人が複数のメールアドレスを使っていても、`contactPageId`が同じなら1人として数える。
知りたいのは「この人はいつ返してくるか」であって、アドレスごとの統計ではないため。

■ 件数を必ず添える

現時点のEmailLogは2026-08-25のGmail連携開始以降しか無く、連絡先1件あたりの返信は
数件しかない（2026-09-03実測: 返信ペアが作れた連絡先は44件中13件、全て4件以下）。
**「14時に返信しやすい」とだけ表示すると、1件の偶然を断定として読ませてしまう。**
そのため`sample_size`・`confidence`・人間可読な`note`を必ず一緒に返し、画面側が
件数を隠せないようにしている。過去分は`scripts/backfill_gmail_history.py`で入れる。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from src.analytics.reply_timing import (
    ContactReplyInsight,
    EmailEvent,
    build_contact_insight,
    format_lag,
)
from src.gmail_sync import db

logger = logging.getLogger(__name__)

_CONFIDENCE_LABELS = {
    "high": "十分",
    "medium": "やや不足",
    "low": "参考値",
    "none": "データなし",
}


def _note(insight: ContactReplyInsight) -> str:
    """何件から出した数字なのかを日本語1行で説明する。"""
    lag_n = insight.lag.sample_size
    timing_n = insight.timing.sample_size
    if lag_n == 0 and timing_n == 0:
        return "このアドレス宛の送受信ログがまだありません。"
    if lag_n == 0:
        return f"受信{timing_n}件のみ。こちらの送信への返信が記録されていないため、返信ラグは出せません。"
    if insight.timing.is_flat:
        return (
            f"返信{lag_n}件・受信{timing_n}件から算出。"
            "受信が時間帯に散らばっていて、返ってきやすい時間帯は見当たりません。"
        )
    if lag_n < 5:
        return f"返信{lag_n}件・受信{timing_n}件から算出。件数が少ないため参考値です。"
    return f"返信{lag_n}件・受信{timing_n}件から算出。"


def _timing_dict(insight: ContactReplyInsight) -> dict[str, Any]:
    timing = insight.timing
    return {
        "sample_size": timing.sample_size,
        "confidence": timing.confidence,
        "confidence_label": _CONFIDENCE_LABELS[timing.confidence],
        "is_flat": timing.is_flat,
        "top_buckets": [{"label": b.label, "count": b.count} for b in timing.top_buckets],
        "top_weekdays": list(timing.top_weekdays),
        "buckets": [{"label": b.label, "count": b.count} for b in timing.buckets],
        "weekday_counts": list(timing.weekday_counts),
    }


def to_dict(insight: ContactReplyInsight) -> dict[str, Any]:
    """`ContactReplyInsight`をJSONで返せる形にする（秒数と、表示用の日本語の両方を返す）。"""
    lag = insight.lag
    return {
        "sample_size": lag.sample_size,
        "confidence": lag.confidence,
        "confidence_label": _CONFIDENCE_LABELS[lag.confidence],
        "median_lag_seconds": lag.median_seconds,
        "median_lag_label": format_lag(lag.median_seconds),
        "mean_lag_seconds": lag.mean_seconds,
        "mean_lag_label": format_lag(lag.mean_seconds),
        "fastest_lag_label": format_lag(lag.fastest_seconds),
        "slowest_lag_label": format_lag(lag.slowest_seconds),
        "inbound_count": insight.inbound_count,
        "outbound_count": insight.outbound_count,
        "last_inbound_at": insight.last_inbound_at.isoformat() if insight.last_inbound_at else None,
        "last_outbound_at": (
            insight.last_outbound_at.isoformat() if insight.last_outbound_at else None
        ),
        "timing": _timing_dict(insight),
        "note": _note(insight),
    }


def build_for_contact_page_ids(page_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """連絡先ページIDのリストから、ページIDをキーにした返信傾向のdictを返す。

    ログが1件も無い連絡先はキー自体が存在しない（画面側は「—」を出せばよい）。

    EmailLogの読み取りに失敗しても360ビュー全体を落とさない（Notion由来の他セクションは
    表示できる、という`clients/[id]/page.tsx`と同じ方針）。失敗時は空dictを返す。
    """
    if not page_ids:
        return {}
    try:
        rows = db.fetch_email_events_by_contact_page_ids(list(page_ids))
    except Exception:
        logger.exception("reply_timing: failed to read EmailLog for %d contacts", len(page_ids))
        return {}

    by_page: dict[str, list[EmailEvent]] = {}
    for row in rows:
        by_page.setdefault(row["contactPageId"], []).append(
            EmailEvent(
                contact_email=row["contactEmail"] or "",
                direction=row["direction"],
                sent_at=row["sentAt"],
                thread_id=row.get("gmailThreadId"),
            )
        )

    return {
        page_id: to_dict(build_contact_insight(page_id, events))
        for page_id, events in by_page.items()
    }
