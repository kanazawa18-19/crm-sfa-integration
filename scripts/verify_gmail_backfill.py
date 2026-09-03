"""Gmailの過去分取り込みの結果を、実件数で確かめる（2026-09-03）。

**取り込みスクリプトの「完了」表示を信用しない。** 実際に何行入り、返信ラグが
何連絡先ぶん出せるようになったかを数える（バックフィル後は実件数を目視確認する、
は運用ルール）。

    set -a; . dashboard/.env.local; set +a
    .venv/bin/python scripts/verify_gmail_backfill.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analytics.reply_timing import EmailEvent, build_insights, format_lag
from src.gmail_sync import db


def main() -> int:
    with db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT count(*) c, min("sentAt") mn, max("sentAt") mx, '
            'count(DISTINCT "contactPageId") contacts FROM "EmailLog"'
        )
        overall = cur.fetchone()
        cur.execute('SELECT direction, count(*) c FROM "EmailLog" GROUP BY 1 ORDER BY 1')
        by_direction = cur.fetchall()
        cur.execute(
            'SELECT "contactPageId", "contactEmail", "gmailThreadId", direction, "sentAt" '
            'FROM "EmailLog" ORDER BY "sentAt"'
        )
        rows = cur.fetchall()

    print("=========== EmailLog の実件数 ===========")
    print(f"  総行数        {overall['c']:,}")
    print(f"  連絡先        {overall['contacts']:,}")
    print(f"  期間          {overall['mn']} 〜 {overall['mx']}（UTC）")
    for r in by_direction:
        label = {"inbound": "受信", "outbound": "送信"}.get(r["direction"], r["direction"])
        print(f"  {label}          {r['c']:,}")

    # 連絡先ページ単位で返信傾向を出し、信頼度の分布を見る。
    by_page: dict[str, list[EmailEvent]] = {}
    for row in rows:
        by_page.setdefault(row["contactPageId"], []).append(
            EmailEvent(
                row["contactEmail"] or "",
                row["direction"],
                row["sentAt"],
                thread_id=row.get("gmailThreadId"),
            )
        )

    from src.analytics.reply_timing import build_contact_insight

    insights = {pid: build_contact_insight(pid, evs) for pid, evs in by_page.items()}
    lag_conf = Counter(i.lag.confidence for i in insights.values())
    timing_conf = Counter(i.timing.confidence for i in insights.values())
    pairs_total = sum(i.lag.sample_size for i in insights.values())

    print()
    print("=========== 返信傾向が出せる連絡先 ===========")
    print(f"  返信ペア合計   {pairs_total:,}")
    print("  返信ラグの信頼度   " + _fmt(lag_conf, len(insights)))
    print("  時間帯の信頼度     " + _fmt(timing_conf, len(insights)))

    top = sorted(insights.values(), key=lambda i: -i.lag.sample_size)[:10]
    if top and top[0].lag.sample_size:
        print()
        print("=========== 返信の多い連絡先（上位10） ===========")
        for i in top:
            if not i.lag.sample_size:
                break
            print(
                f"  n={i.lag.sample_size:<4} 中央値={format_lag(i.lag.median_seconds):>10}"
                f"  平均={format_lag(i.lag.mean_seconds):>10}"
                f"  時間帯={'/'.join(b.label for b in i.timing.top_buckets) or '—'}"
            )
    return 0


def _fmt(counter: Counter, total: int) -> str:
    order = ["high", "medium", "low", "none"]
    labels = {"high": "十分", "medium": "やや不足", "low": "参考値", "none": "データなし"}
    parts = [f"{labels[k]} {counter.get(k, 0)}" for k in order]
    return "  ".join(parts) + f"（連絡先 {total}件）"


if __name__ == "__main__":
    raise SystemExit(main())
