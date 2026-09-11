#!/usr/bin/env python3
"""チェックイン機の導入有無をWEB検索で確かめるCLI(2026-09-11)。

楽天トラベルの施設ページにはチェックイン機のことがほとんど書かれていない
（2026-09-11、鳥取県40施設で実測して**40件すべて記載なし**）。そのため
WEB検索での確認を別工程として用意した。

**1件ごとにClaude APIを呼ぶ（有料）。** 既定では上限50件で止まり、それ以上は
明示的に `--limit` を上げないと動かない。既に判定済みの施設は再検索しない。

使い方:
    # まず何件が対象になるか見る（APIは呼ばない）
    python scripts/search_check_in_machines.py --prefecture 鳥取県 --dry-run

    # 実際に検索する（Claude APIを呼ぶ・課金される）
    python scripts/search_check_in_machines.py --prefecture 鳥取県 --limit 20

実行には環境変数 DATABASE_URL と ANTHROPIC_API_KEY が必要。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.facility_list.domain.models import CheckInMachineStatus
from src.facility_list.infrastructure import db as facility_db
from src.facility_list.infrastructure.check_in_machine_search import search_check_in_machine

logger = logging.getLogger("search_check_in_machines")

# 既定の上限。うっかり数千件にAPIを呼ばないための歯止め。
DEFAULT_LIMIT = 50


def main() -> int:
    parser = argparse.ArgumentParser(description="チェックイン機の導入有無をWEB検索で確かめる")
    parser.add_argument("--prefecture", action="append", help="対象の都道府県(複数指定可)")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"検索する施設数の上限(既定{DEFAULT_LIMIT})"
    )
    parser.add_argument("--min-rooms", type=int, default=None, help="この客室数以上の施設だけ")
    parser.add_argument("--dry-run", action="store_true", help="対象件数を数えるだけ(APIを呼ばない)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    facilities = facility_db.fetch_facilities(prefectures=args.prefecture, only_listed=True)

    # 既に判定済み(yes/no)のものは再検索しない。unknownは、一次判定で見つからなかった
    # だけかもしれないので対象に含める。
    targets = [f for f in facilities if f.check_in_machine is CheckInMachineStatus.UNKNOWN]
    if args.min_rooms is not None:
        targets = [f for f in targets if f.room_count is not None and f.room_count >= args.min_rooms]

    logger.info("母集団%d件 / 未確定%d件", len(facilities), len(targets))

    if args.dry_run:
        logger.info("[dry-run] 検索対象は%d件（上限%d件）。APIは呼んでいない", len(targets), args.limit)
        return 0

    targets = targets[: args.limit]
    counts = {"yes": 0, "no": 0, "unknown": 0}
    not_searched = 0

    for index, facility in enumerate(targets, start=1):
        result = search_check_in_machine(facility)
        if not result.searched:
            not_searched += 1
            logger.warning("  [%d/%d] %s: 検索できず（%s）", index, len(targets), facility.name, result.not_searched_reason)
            # APIキーが無い等、全件で同じ理由なら回し続けても意味がない。
            if not_searched >= 3 and counts == {"yes": 0, "no": 0, "unknown": 0}:
                logger.error("続けて検索できないので中止する: %s", result.not_searched_reason)
                return 2
            continue

        counts[result.status.value] += 1
        facility_db.upsert_check_in_machine(
            facility.hotel_no,
            status=result.status,
            source=result.source,
            evidence=result.evidence,
            evidence_url=result.evidence_url,
        )
        logger.info(
            "  [%d/%d] %s → %s %s",
            index,
            len(targets),
            facility.name,
            result.status.value,
            (result.evidence or "")[:60],
        )

    logger.info(
        "完了: あり%d件 / なし%d件 / 不明%d件 / 検索できず%d件",
        counts["yes"],
        counts["no"],
        counts["unknown"],
        not_searched,
    )
    return 2 if not_searched else 0


if __name__ == "__main__":
    raise SystemExit(main())
