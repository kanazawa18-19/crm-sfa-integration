"""スプレッドシートの行をまとめて作る（2026-08-31）。

**なぜ専用スクリプトなのか。**
リアルタイム同期の経路（Webhook）で6万件を埋めようとすると、レコードが次に更新される
まで行が作られない。しかも一度に大量のイベントが来ればGoogle Sheets APIのQuotaに当たる。
レビューでも「dedicated backfill job → 検証 → リアルタイム作成ON」の順が安全だと
指摘された（2026-08-31、ChatGPT）。

**書き込みは`--apply`を付けたときだけ。** 既定はdry-runで、何件作られるかを数えるだけ。

冪等。既にシートに同期キーがある行は作り直さず、行番号だけ`IdMapping`へ入れ直す。
途中で止めても、もう一度流せば続きから埋まる。

    # まず試算（書き込まない）
    python scripts/backfill_spreadsheet_rows.py --db-key product
    # 少数で実際に試す
    python scripts/backfill_spreadsheet_rows.py --db-key product --limit 3 --apply
    # 全部
    python scripts/backfill_spreadsheet_rows.py --db-key product --apply
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.production_wiring import (
    build_id_mapping_store,
    build_notion_clients_by_db,
    build_spreadsheet_targets_by_db,
)

logger = logging.getLogger("backfill_spreadsheet_rows")

#: 連続で叩き続けてGoogle Sheets APIのQuota（ユーザーあたり100リクエスト/100秒）に
#: 当たらないよう、1件ごとに少し待つ。1件につきヘッダ取得＋追記で2〜3リクエスト。
_SLEEP_SECONDS = 0.4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-key", required=True, help="対象のDB（例: product）")
    parser.add_argument("--limit", type=int, default=None, help="処理する件数の上限")
    parser.add_argument(
        "--apply", action="store_true", help="実際に書き込む（付けなければ試算のみ）"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    schema = get_schema(args.db_key)
    targets = build_spreadsheet_targets_by_db()
    target = targets.get(args.db_key)
    if target is None:
        logger.error(
            "db_key=%r 用のスプレッドシート同期が構築できません"
            "（SPREADSHEET_ID/Google認証情報を確認してください）",
            args.db_key,
        )
        return 1

    notion_clients = build_notion_clients_by_db()
    notion = notion_clients.get(args.db_key)
    if notion is None:
        logger.error("db_key=%r 用のNotionクライアントが構築できません", args.db_key)
        return 1

    store = build_id_mapping_store()
    mappings = store.list_by_db(args.db_key)
    if args.limit is not None:
        mappings = mappings[: args.limit]

    # スプレッドシートへ流すプロパティだけに絞る（kintone/Zohoには一切触らない）。
    sync_targets = [p.name for p in schema.properties if p.sync_scope.includes(Tool.SPREADSHEET)]
    logger.info(
        "db_key=%s / 対象マッピング=%d件 / シート「%s」へ流す項目=%d個 / %s",
        args.db_key,
        len(mappings),
        schema.spreadsheet_sheet_name,
        len(sync_targets),
        "実行" if args.apply else "試算のみ（--applyで実行）",
    )

    created = existing = skipped = failed = 0
    for index, mapping in enumerate(mappings, start=1):
        try:
            row = target.find_row_by_sync_key(mapping.notion_key)
            if row is not None:
                existing += 1
                if args.apply and mapping.spreadsheet_row != row:
                    store.upsert(dataclasses.replace(mapping, spreadsheet_row=row))
                continue

            page = notion.get_page(mapping.notion_key)
            if page is None:
                logger.warning("Notionページが読めませんでした: %s", mapping.notion_key)
                skipped += 1
                continue

            values = {name: page[name] for name in sync_targets if name in page}
            if not values:
                skipped += 1
                continue

            if not args.apply:
                created += 1
                continue

            new_row = target.append_row_with_sync_key(values, mapping.notion_key)
            store.upsert(dataclasses.replace(mapping, spreadsheet_row=new_row))
            created += 1
            time.sleep(_SLEEP_SECONDS)
        except Exception:  # noqa: BLE001 - 1件の失敗で全体を止めない
            logger.exception("失敗しました: %s", mapping.notion_key)
            failed += 1

        if index % 20 == 0:
            logger.info("進捗 %d/%d", index, len(mappings))

    logger.info(
        "%s: 作成=%d / 既存=%d / スキップ=%d / 失敗=%d",
        "完了" if args.apply else "試算完了",
        created,
        existing,
        skipped,
        failed,
    )
    # **「成功しました」で終わらせない。**実際の行数は呼び出し側で数えること。
    logger.info("シートの実件数は `count_rows` か画面で必ず確認すること")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
