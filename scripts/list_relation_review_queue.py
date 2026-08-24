#!/usr/bin/env python3
"""RelationReviewQueue（リレーション解決が曖昧だったため人の確認待ちになっているレコード）
を一覧表示する読み取り専用CLI（2026-08-25）。

■ 何のためのスクリプトか -----------------------------------------------------------------
`src/relation_sync/resolve.py`の`resolve_client_master_relation()`は、kintone等から届いた
会社名テキストを取引先マスターDBのNotion page IDへ解決できなかった場合（候補0件・複数件）、
自動では確定させず`RelationReviewQueue`へpending状態で記録するだけに留める（誤った
リレーションを機械的に確定させるより安全なため）。しかし`list_pending_reviews()`
（`src/relation_sync/review_queue.py`）を呼ぶ手段がこれまで無く、記録されるだけで誰も
確認しないまま埋もれてしまう状態だった（shirokuma-sec/obasan-qualityレビューBLOCKER対応）。
本スクリプトはpending状態の一覧を人が読める形で出力するだけで、書き込みは一切行わない
（resolved/dismissedへの遷移は今回のスコープでは扱わず、手動SQL操作を前提とする。
`docs/relation_sync_activation_note.md`参照）。

■ 使い方 -----------------------------------------------------------------------------------
    python scripts/list_relation_review_queue.py

実行には環境変数 DATABASE_URL（RelationReviewQueueの読み取り元Neon Postgres）が必要。
"""

from __future__ import annotations

import sys
from itertools import zip_longest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.relation_sync.review_queue import list_pending_reviews


def _format_candidates(review: dict[str, Any]) -> str:
    """候補一覧を「会社名(page ID)」の形で人が読める形式にする。

    `candidateRawNames`は`candidateNotionPageIds`と同じ順序・同じ件数で対になる想定だが
    （`src/relation_sync/resolve.py`参照）、本カラム追加前に記録された既存行では
    `candidateRawNames`が空の可能性があるため、`zip_longest`で欠損側を補いながら表示する
    （shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-25: page IDのみの羅列だと
    運用者が毎回Notionを開いて確認する必要があり、このスクリプト本来の目的を果たせない
    ため、候補の実体（会社名）も表示できるようにした）。
    """
    page_ids = review.get("candidateNotionPageIds") or []
    raw_names = review.get("candidateRawNames") or []
    if not page_ids:
        return "候補なし"
    pairs = [
        f"{name or '(名称不明)'}({page_id or '(ID不明)'})"
        for page_id, name in zip_longest(page_ids, raw_names)
    ]
    return f"候補{len(pairs)}件: {', '.join(pairs)}"


def print_report(reviews: list[dict[str, Any]]) -> None:
    if not reviews:
        print("pending状態のRelationReviewQueueはありません。")
        return
    print(f"pending状態のRelationReviewQueue: {len(reviews)}件\n")
    for review in reviews:
        print(
            f"- id={review['id']} createdAt={review['createdAt']}\n"
            f"    sourceTool={review['sourceTool']!r} sourceRecordId={review['sourceRecordId']!r} "
            f"targetDbKey={review['targetDbKey']!r}\n"
            f"    rawValue={review['rawValue']!r}\n"
            f"    {_format_candidates(review)}"
        )


def main() -> None:
    reviews = list_pending_reviews()
    print_report(reviews)


if __name__ == "__main__":
    main()
