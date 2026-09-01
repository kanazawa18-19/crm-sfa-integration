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

from typing import Any

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import (
    PARSEABLE_NOTION_PROPERTY_TYPES,
    parse_notion_property_value,
)
from src.sync_engine.sync_targets.spreadsheet_sync import SYNC_KEY_COLUMN
from src.sync_engine.production_wiring import (
    build_id_mapping_store,
    build_notion_clients_by_db,
    build_spreadsheet_targets_by_db,
)

logger = logging.getLogger("backfill_spreadsheet_rows")

#: まとめて追記する件数。Sheetsのappendは複数行を1リクエストで受け付ける。
#: 大きくしすぎると失敗時のやり直しが重くなるので、ほどほどにする。
_APPEND_BATCH_SIZE = 200


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
    # **件数がキリのいい数字なら疑う**（2026-09-01の教訓）。Notionの Database Query は
    # 1クエリ1万件で打ち切るのに `has_more: false` を返すため、取りこぼしが「全部取れた」に
    # 見える。`_query_all()`はキーセット方式に直したが、ちょうど1万件で止まっていたら
    # まだどこかで壁に当たっている。黙って9割欠けたまま流すより、ここで気づける形にする。
    if args.limit is None and len(mappings) == 10_000:
        logger.error(
            "対象マッピングがちょうど10,000件です。Notionの「1クエリ1万件」の壁に"
            "当たっている可能性が高い（`src/sync_engine/clients/_notion_paging.py`参照）。"
            "**この件数で流すと9割が抜けたままになります。** 先に件数を数え直してください。"
            "打ち切ります（rc=2）"
        )
        # **ログに1行出すだけの失敗は、見ないので無かったのと同じ。**
        # ここで`return`しないと、呼び出し元の`backfill_spreadsheet_all.py`はrcしか見ないため
        # 「OK」と表示して次のDBへ進んでしまう（2026-09-01、レビュー指摘）。
        return 2

    if mappings:
        # 同期キー列を1回だけ読み込む。これをしないと1件ごとに列を全読みしてO(n²)になり、
        # 実測で1件あたり5.6秒かかった（2026-08-31）。バックフィル中はこのシートへ
        # 書くのが自分だけなので、以後キャッシュを正として扱ってよい。
        try:
            known = target._client.prime_sync_key_rows(
                schema.spreadsheet_sheet_name, SYNC_KEY_COLUMN
            )
            logger.info("同期キーを先読みしました: 既存%d件", known)
        except Exception:
            logger.warning("同期キーの先読みに失敗しました。そのまま続行します", exc_info=True)

    if args.apply and mappings:
        # 既定のシートは1000行しかない。追記で自動的に伸びることを当てにせず、
        # 流す件数が分かっているここで先に広げておく（2026-08-31、実行して判明）。
        try:
            target._client.ensure_row_capacity(
                schema.spreadsheet_sheet_name, len(mappings) + 10
            )
        except Exception:
            logger.warning("行数の事前拡張に失敗しました。そのまま続行します", exc_info=True)

    # Notionのページは**1件ずつ取らない**（2026-08-31）。get_page()を件数分呼ぶと
    # NotionのAPIレート（およそ3リクエスト/秒）で頭打ちになる。DB全件を100件ずつの
    # クエリで先に読んでおけば、3781件で38リクエストで済む。
    pages_by_id: dict[str, dict[str, Any]] = {}
    if mappings:
        try:
            for raw in notion.query_all_pages():
                parsed = {}
                for name, value in (raw.get("properties") or {}).items():
                    if value.get("type") not in PARSEABLE_NOTION_PROPERTY_TYPES:
                        continue
                    parsed[name] = parse_notion_property_value(value)
                pages_by_id[str(raw.get("id"))] = parsed
            logger.info("Notionページを一括取得しました: %d件", len(pages_by_id))
        except Exception:
            logger.warning(
                "Notionページの一括取得に失敗しました。1件ずつ取りに行きます", exc_info=True
            )

    created = existing = skipped = failed = 0
    pending: list[tuple[Any, dict[str, Any]]] = []

    def _flush() -> None:
        """溜めた行をまとめて追記し、採番された行番号をIdMappingへ入れる。

        Sheetsの書き込みQuotaは100リクエスト/100秒。1行ずつ追記すると1秒に1行しか
        書けず、3万件で18時間かかる。appendは複数行を1リクエストで受け付ける。
        """
        nonlocal created, failed
        if not pending:
            return
        batch = list(pending)
        pending.clear()
        try:
            rows = target._client.append_rows(
                schema.spreadsheet_sheet_name,
                [target.with_sync_key(values, m.notion_key) for m, values in batch],
            )
        except Exception:
            logger.exception("まとめ追記に失敗しました（%d件）", len(batch))
            failed += len(batch)
            return
        for (m, _values), row in zip(batch, rows):
            target._client.remember_sync_key_row(
                schema.spreadsheet_sheet_name, m.notion_key, row
            )
            try:
                store.upsert(dataclasses.replace(m, spreadsheet_row=row))
                created += 1
            except Exception:
                logger.exception("行番号の記録に失敗しました: %s", m.notion_key)
                failed += 1

    for index, mapping in enumerate(mappings, start=1):
        try:
            row = target.find_row_by_sync_key(mapping.notion_key)
            if row is not None:
                existing += 1
                if args.apply and mapping.spreadsheet_row != row:
                    store.upsert(dataclasses.replace(mapping, spreadsheet_row=row))
                continue

            page = pages_by_id.get(mapping.notion_key)
            if page is None:
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

            pending.append((mapping, values))
            if len(pending) >= _APPEND_BATCH_SIZE:
                _flush()
        except Exception:  # noqa: BLE001 - 1件の失敗で全体を止めない
            logger.exception("失敗しました: %s", mapping.notion_key)
            failed += 1

        if index % 200 == 0:
            logger.info("進捗 %d/%d", index, len(mappings))

    _flush()

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
