"""Notionの「1クエリ1万件」の壁を越えて全件取る（2026-09-01）。

■ 何が起きていたか

Notion の Database Query は、**1クエリあたり1万件までしか返さない。**
しかも打ち切るときに `has_more: false` を返すため、呼び出し側からは
**「全部取れた」ように見える。** エラーも警告も出ない。

実測（`config`のIDマッピングDB、db_key=client_master）:

    絞り込みなし              10000件（has_more=false）
    先頭2文字で分割して合計    10049件   ← 1万を超えた

金沢さんが「ダッシュボードの案件数が10000になっている」と気づかなければ、
そのままだった。案件管理のPostgresミラー・取引先名インデックス（名寄せ）も
同じ取り方をしており、静かに欠けていた。

■ どう越えるか

**作成日時の昇順に並べ、最後に見た作成日時から先を取り直す**（キーセット方式）。
1周で1万件取れたら、まだ先があるとみなしてもう1周する。

- 境界は `on_or_after` で重ねて取り、**ページIDで重複を除く**。
  `after` にすると、同じ秒に作られたレコードを取りこぼす
- 同じ作成日時が1万件を超えると前へ進めない。そのときは**打ち切らずに警告を出す**
  （黙って欠けるのが今回の問題そのものなので、気づける形で止める）
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

#: Notionが1クエリで返す上限。これに達したら「まだ先がある」とみなす。
QUERY_RESULT_CAP = 10_000

#: 上限ちょうどでなくても、近ければ次の周を回す（将来上限が変わっても取りこぼさない）。
_NEXT_ROUND_THRESHOLD = 9_000


def query_all_with_keyset(
    post_query: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    base_filter: dict[str, Any] | None = None,
    page_size: int = 100,
    label: str = "",
) -> list[dict[str, Any]]:
    """1万件の壁を越えて全件返す。

    `post_query`はリクエストbodyを受け取り、Notionの応答（`results`/`has_more`/
    `next_cursor`を持つ辞書）を返す関数。HTTPの都合は呼び出し元が持つ。
    """
    collected: dict[str, dict[str, Any]] = {}
    watermark: str | None = None

    while True:
        round_count = 0
        newest = watermark
        cursor: str | None = None

        while True:
            body: dict[str, Any] = {
                "page_size": page_size,
                "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            }
            conditions = list((base_filter or {}).get("and", []) or ([base_filter] if base_filter else []))
            if watermark is not None:
                conditions.append(
                    {"timestamp": "created_time", "created_time": {"on_or_after": watermark}}
                )
            if conditions:
                body["filter"] = conditions[0] if len(conditions) == 1 else {"and": conditions}
            if cursor is not None:
                body["start_cursor"] = cursor

            data = post_query(body)
            results = data.get("results") or []
            for page in results:
                page_id = page.get("id")
                if page_id:
                    collected[str(page_id)] = page
                created = page.get("created_time")
                if created:
                    newest = created
            round_count += len(results)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                # Notion APIの契約上は起きないはずの応答。先頭ページの取り直しを繰り返す
                # 無限ループを避けて打ち切る（`HttpNotionClient.query_all_pages`と同じ対応）。
                logger.warning(
                    "query_all_with_keyset: has_more=True なのに next_cursor が空です"
                    "（label=%r）。ここで打ち切ります",
                    label,
                )
                break

        if round_count < _NEXT_ROUND_THRESHOLD:
            break
        if newest == watermark:
            # 同じ作成日時が上限ぶん並んでいて前へ進めない。**黙って欠けさせない。**
            logger.error(
                "query_all_with_keyset: 同じ作成日時が上限ぶん並んでおり、これ以上進めません"
                "（label=%r, 取得済み=%d件, created_time=%r）。**取りこぼしています。**",
                label,
                len(collected),
                watermark,
            )
            break
        logger.info(
            "query_all_with_keyset: 1万件の上限に達したので続きを取ります"
            "（label=%r, ここまで%d件, 次は %s 以降）",
            label,
            len(collected),
            newest,
        )
        watermark = newest

    return list(collected.values())
