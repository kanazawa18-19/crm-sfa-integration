#!/usr/bin/env python3
"""楽天トラベルの公開ページから宿泊施設を取り込むCLI(2026-09-11)。

リスト作成マシーンの母集団を作る。**楽天ウェブサービス(API)は使わない**
(利用規約第10条(4)に触れる可能性があるため、2026-09-11に本人判断で公開ページ方式)。

取り込みは2段階:

    --shallow-only  エリア一覧だけ(30件/ページ)。施設名・クチコミ点数・住所・最安料金
    既定            一覧で見つけた施設の施設ページ2本も取りに行き、客室数・館内設備・
                    カスタマイズページの有無・温泉まで埋める

**まず1県で試すこと。** 全国43,753施設を一度に回すと、楽天への負荷としても
所要時間としても過大になる。

使い方:
    # 鳥取県を丸ごと（詳細まで）
    python scripts/import_rakuten_facilities.py --prefecture 鳥取県

    # まず動作を見たいとき（一覧1ページ＝30件だけ、DBに書かない）
    python scripts/import_rakuten_facilities.py --prefecture 鳥取県 --max-pages 1 --dry-run

    # 一覧だけ全県（詳細は後で）
    python scripts/import_rakuten_facilities.py --all-prefectures --shallow-only

実行には環境変数 DATABASE_URL が必要(--dry-run なら不要)。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.facility_list.application.import_facilities import (
    enrich_facilities,
    import_prefecture_shallow,
)
from src.facility_list.infrastructure import db as facility_db
from src.facility_list.infrastructure.rakuten_client import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_WORKERS,
    PREFECTURE_SLUGS,
    RakutenTravelClient,
)

logger = logging.getLogger("import_rakuten_facilities")


def main() -> int:
    parser = argparse.ArgumentParser(description="楽天トラベルの公開ページから施設を取り込む")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--prefecture", help="取り込む都道府県(例: 鳥取県)")
    target.add_argument(
        "--all-prefectures", action="store_true", help="47都道府県すべて(時間がかかる)"
    )
    target.add_argument(
        "--show-total",
        action="store_true",
        help="サイトマップから掲載施設の総数だけ数える(取り込みはしない)",
    )
    parser.add_argument("--max-pages", type=int, default=None, help="一覧の最大ページ数(30件/ページ)")
    parser.add_argument(
        "--shallow-only", action="store_true", help="施設ページを取りに行かず一覧だけで終える"
    )
    parser.add_argument("--limit", type=int, default=None, help="詳細を取る施設数の上限")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"リクエスト間隔(秒、既定{DEFAULT_INTERVAL_SECONDS})。短くしすぎない",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_MAX_WORKERS, help="詳細取得の同時実行数"
    )
    parser.add_argument("--dry-run", action="store_true", help="DBに書かず件数だけ出す")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.show_total:
        # 母集団の全体像を知るためだけの経路。1〜2リクエストで終わる。
        client = RakutenTravelClient(interval_seconds=args.interval)
        numbers = client.fetch_all_hotel_numbers()
        logger.info("楽天トラベルの掲載施設: %d件（サイトマップより）", len(numbers))
        return 0

    if args.prefecture and args.prefecture not in PREFECTURE_SLUGS:
        logger.error("未知の都道府県: %s", args.prefecture)
        return 1

    prefectures = list(PREFECTURE_SLUGS) if args.all_prefectures else [args.prefecture]
    client = RakutenTravelClient(interval_seconds=args.interval)

    total_written = 0
    total_failed = 0
    unhealthy = False
    started = time.monotonic()

    for prefecture in prefectures:
        logger.info("=== %s ===", prefecture)
        result = import_prefecture_shallow(prefecture, client=client, max_pages=args.max_pages)

        if not args.shallow_only:
            targets = result.facilities[: args.limit] if args.limit else result.facilities
            result = enrich_facilities(
                targets, client=client, max_workers=args.workers, result=result
            )

        unlisted = [f.hotel_no for f in result.facilities if not f.is_listed]
        listed = [f for f in result.facilities if f.is_listed]

        if args.dry_run:
            logger.info(
                "[dry-run] %s: 書き込み対象%d件 / 掲載終了%d件 / 失敗%d件",
                prefecture,
                len(listed),
                len(unlisted),
                result.failed_count,
            )
        elif not result.is_healthy:
            # **健全でないときは掲載終了の印を付けない。** 楽天側のHTML変更で
            # 施設名が読めなくなっただけの施設を、営業リストから静かに消して
            # しまうため(shirokuma-secレビュー指摘、2026-09-11)。
            # 取れた分の上書きだけ行い、掲載終了の判定は人が確かめるまで保留する。
            written = facility_db.upsert_facilities(
                listed, parse_warnings=result.parse_warnings
            )
            total_written += written
            logger.error(
                "[%s] %s。掲載終了の印付け(%d件)は保留した",
                prefecture,
                result.health_warning,
                len(unlisted),
            )
        else:
            written = facility_db.upsert_facilities(
                listed, parse_warnings=result.parse_warnings
            )
            facility_db.mark_unlisted(unlisted)
            total_written += written

        total_failed += result.failed_count

        # rc=0を信用しない。読めなかった割合が高ければ、件数が出ていても警告する。
        if not result.is_healthy:
            unhealthy = True
            logger.error("[%s] %s。楽天側のHTML変更を疑うこと", prefecture, result.health_warning)
        for hotel_no, reason in result.failures[:10]:
            logger.warning("  取得失敗 hotelNo=%s: %s", hotel_no, reason)

    elapsed = time.monotonic() - started
    logger.info(
        "完了: 書き込み%d件 / 失敗%d件 / 所要%.1f分", total_written, total_failed, elapsed / 60
    )
    # 失敗や異常が出ていたら終了コードを分ける(ログ1行では気づけないため)。
    return 2 if (total_failed or unhealthy) else 0


if __name__ == "__main__":
    raise SystemExit(main())
