#!/usr/bin/env python3
"""IdMappingStore内で、外部ID（kintone_id/zoho_id/spreadsheet_row）が異なるdb_keyへ
またがって重複していないかを横断的に検査する（読み取り専用、書き込みは一切行わない）。

■ 何のためのスクリプトか -----------------------------------------------------------------
2026-08-14、`IdMappingStore.find_by_external_id()`がdb_keyを無視して外部IDだけで検索して
いたことが原因で、別db_key（別kintoneアプリ）の同番号レコードを取り違える設計バグが発覚した
（`docs/kintone_webhook_activation_note.md`「問題3」参照）。コード側は修正済みだが、この
バグは2026-08-11から本番稼働していた「他ツール→kintone」書き込み経路にも存在していたため、
過去に紛れ込んだ可能性のある誤マッピング・誤書き込みをコード修正だけでは検出できない。
本スクリプトは`IdMappingStore.list_by_db()`で全db_keyのマッピングを取得し、同一ツールの
同一外部ID値が複数db_keyにまたがっていないかを機械的にチェックする。

重複が見つかった場合、それ自体が即座に「データが壊れている」ことを意味するわけではない
（IDマッピングの記録が重複しているだけの場合と、実際にkintone側へ誤った値が書き込まれて
いる場合がある）。重複が見つかったペアについては、該当のkintone/Zoho/スプレッドシート側の
更新履歴を個別に確認すること。

■ 使い方 -----------------------------------------------------------------------------------
    # 本番のNotion裏付けIdMappingStoreを対象にする場合
    # （SYNC_ID_MAPPING_NOTION_API_KEY等、本番同等の環境変数が必要）
    python scripts/audit_id_mapping_collisions.py

    # 対象db_keyを絞り込む場合
    python scripts/audit_id_mapping_collisions.py --db-key project --db-key action
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.id_mapping import IdMappingStore
from src.sync_engine.production_wiring import build_id_mapping_store

_ALL_DB_KEYS = tuple(schema.key for schema in ALL_SCHEMAS)

_EXTERNAL_ID_FIELDS: tuple[tuple[Tool, str], ...] = (
    (Tool.KINTONE, "kintone_id"),
    (Tool.ZOHO, "zoho_id"),
    (Tool.SPREADSHEET, "spreadsheet_row"),
)


@dataclass(frozen=True)
class Collision:
    tool: Tool
    external_id: str
    notion_keys_by_db_key: dict[str, str]


def find_cross_db_key_collisions(
    store: IdMappingStore, db_keys: tuple[str, ...] = _ALL_DB_KEYS
) -> list[Collision]:
    """指定db_key群を横断して、同一ツールの同一外部ID値が複数db_keyに存在するケースを返す。"""
    # tool -> external_id値 -> {db_key: notion_key}
    seen: dict[Tool, dict[str, dict[str, str]]] = {tool: defaultdict(dict) for tool, _ in _EXTERNAL_ID_FIELDS}

    for db_key in db_keys:
        for mapping in store.list_by_db(db_key):
            for tool, field_name in _EXTERNAL_ID_FIELDS:
                value = getattr(mapping, field_name)
                if value is None:
                    continue
                seen[tool][str(value)][db_key] = mapping.notion_key

    collisions: list[Collision] = []
    for tool, _ in _EXTERNAL_ID_FIELDS:
        for external_id, notion_keys_by_db_key in seen[tool].items():
            if len(notion_keys_by_db_key) > 1:
                collisions.append(
                    Collision(tool=tool, external_id=external_id, notion_keys_by_db_key=dict(notion_keys_by_db_key))
                )
    return collisions


def print_report(collisions: list[Collision], *, db_keys: tuple[str, ...]) -> None:
    print(f"対象db_key: {', '.join(db_keys)}")
    if not collisions:
        print("重複なし。db_keyをまたいだ外部ID衝突は見つかりませんでした。")
        return
    print(f"{len(collisions)}件の衝突が見つかりました:\n")
    for c in collisions:
        print(f"- {c.tool.value} external_id={c.external_id!r}")
        for db_key, notion_key in sorted(c.notion_keys_by_db_key.items()):
            print(f"    db_key={db_key!r} notion_key={notion_key!r}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-key",
        action="append",
        dest="db_keys",
        help="対象db_keyを絞り込む（複数指定可）。未指定時は全db_keyが対象。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    db_keys = tuple(args.db_keys) if args.db_keys else _ALL_DB_KEYS

    store = build_id_mapping_store()
    collisions = find_cross_db_key_collisions(store, db_keys)
    print_report(collisions, db_keys=db_keys)


if __name__ == "__main__":
    main()
