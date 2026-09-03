"""未返信メールリマインドの本体(2026-08-16)。

対象は「連絡先からの受信メールで、その後まだ返信していないもの」全件
(`db.find_latest_inbound_awaiting_reply()`が`EmailLog`側で判定する — ある`contactPageId`の
最新行が`direction="inbound"`のケース)。新しい受信/送信メールが記録されて最新行が
変われば、古い受信メールへのリマインド判定は自然にされなくなる。

閾値は`AppSettings.emailReminderThresholdHours`(3時間刻み、3〜72時間、管理者が選択式で
有効にする)。各対象について、経過時間以下で最大の閾値(＝直近でクロスした閾値)のみを
対象にリマインドする。

同じ受信メール1件・同じ閾値での二重送信は、`db.record_reminder_sent()`が
`EmailReminderLog`の一意制約(`@@unique([emailLogId, thresholdHours])`)へ
`ON CONFLICT DO NOTHING`でアトミックに記録を試み、実際に新規記録できた場合のみ
Slack DMを送る、という順序で防ぐ(shirokuma-secレビューWARN対応、2026-08-16)。
先にチェックしてから送信・記録する順序だと、GitHub Actionsの多重起動時にチェックと
記録の間で競合し二重送信になりうるため、「送信の権利」を先にDB側でアトミックに
獲得してから送る設計にしている(詳細は`db.record_reminder_sent()`のdocstring参照)。

`GET /api/cron/email-reminder-check`(GitHub Actionsから1時間おき)から呼ばれる想定。
新規env変数は無い(`SLACK_BOT_TOKEN`・`DATABASE_URL`は既存を流用)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.email_reminders import db, slack_notify

logger = logging.getLogger(__name__)


# これより古い受信メールにはリマインドしない(2026-09-03)。
#
# 閾値の上限が72時間である以上、3か月前の受信メールに対しても「72時間クロス」として
# リマインドが飛ぶ。**今さらDMされても行動には繋がらず、ノイズにしかならない。**
# 加えて`scripts/backfill_gmail_history.py`で過去数か月分を取り込むと、それまで
# `EmailLog`に1行も無かった連絡先の「最新行」が一気に古い受信メールになりうるため、
# この上限が無いと取り込み直後に大量のリマインドDMが一斉に飛ぶ。
_MAX_REMINDER_AGE_HOURS = 14 * 24


def _elapsed_hours(sent_at: datetime, *, now: datetime) -> float:
    """`sent_at`(psycopg経由、`TIMESTAMP(3)`列 — タイムゾーン情報を持たないUTC値として
    保存されている)からの経過時間を時間単位で返す。"""
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return (now - sent_at) / timedelta(hours=1)


def _threshold_to_notify(elapsed_hours: float, thresholds: list[int]) -> int | None:
    """経過時間以下で最大の閾値(＝直近でクロスした閾値)を返す。該当が無ければNone。"""
    crossed = [t for t in thresholds if t <= elapsed_hours]
    if not crossed:
        return None
    return max(crossed)


def run_reminder_check() -> dict[str, int]:
    """未返信メールのリマインド判定・送信を1回実行する。サマリを返す。"""
    enabled, thresholds = db.get_reminder_settings()
    if not enabled:
        return {"disabled": 1}

    candidates = db.find_latest_inbound_awaiting_reply()
    now = datetime.now(timezone.utc)

    sent = 0
    failed = 0
    skipped_too_old = 0
    for row in candidates:
        try:
            elapsed_hours = _elapsed_hours(row["sentAt"], now=now)
            if elapsed_hours > _MAX_REMINDER_AGE_HOURS:
                # 古すぎる受信メール(上記`_MAX_REMINDER_AGE_HOURS`参照)。
                skipped_too_old += 1
                continue
            threshold = _threshold_to_notify(elapsed_hours, thresholds)
            if threshold is None:
                continue

            # 送信の権利を先にDBでアトミックに獲得してから送る(モジュールdocstring・
            # db.record_reminder_sent()参照)。既に記録済み(=他の実行が先に処理済み)なら
            # 送信せずスキップする。
            newly_recorded = db.record_reminder_sent(row["id"], threshold)
            if not newly_recorded:
                continue

            slack_notify.send_reminder_dm(
                rep_email=row["repEmail"],
                contact_email=row["contactEmail"],
                hours_elapsed=int(elapsed_hours),
                subject=row.get("subject"),
            )
            sent += 1
        except Exception:
            # 1件(担当者)の失敗が他を止めないよう、対象ごとにtry/exceptで独立させる
            # (gmail_sync.sync.sync_all()と同じ方針)。
            logger.exception(
                "email_reminders: failed to process reminder for email_log_id=%s", row.get("id")
            )
            failed += 1

    return {
        "eligible": len(candidates),
        "sent": sent,
        "failed": failed,
        "skipped_too_old": skipped_too_old,
    }
