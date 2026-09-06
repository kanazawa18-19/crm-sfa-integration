"""シートの1行へ流してよい項目を選ぶ規則（2026-09-07に`dispatcher.py`から独立）。

「シートへ流してよい項目とは何か」は業務ルール（Domain側の関心事）で、書き込み経路の
都合とは別に1箇所へ置く。詳しくは`spreadsheet_row_properties()`のdocstring。
"""

from __future__ import annotations

from typing import Any, Container, Mapping

from src.db_schema.base import Tool
from src.sync_engine.sync_targets.spreadsheet_sync import drop_relation_properties


def spreadsheet_row_properties(
    source: Mapping[str, Any],
    schema: Any,
    db_key: str,
    *,
    exclude: Container[str] = (),
) -> dict[str, Any]:
    """`source`から**シートの1行に流してよい項目だけ**を取り出す（2026-09-03に一本化）。

    「シートへ流してよい項目とは何か」は業務ルールで、散らすと片方だけ直して
    片方を忘れる（2026-09-03、おばさん指摘）。**この関数が唯一の正本。** 使うのは次の3つ。

    ```
       _spreadsheet_properties_for_new_row()          既にあるレコードの行を作るとき
         source=Notionページの現在値 / exclude=イベントで既に入っている項目

       _append_spreadsheet_row_for_created_record()   新規作成でその場の行を作るとき
         source=Notionページを作るのに使った全項目 / exclude=なし

       spreadsheet_outbox_drain.py                    後から作り直すとき（2026-09-07）
         source=Notionページの現在値 / exclude=なし
    ```

    **`dispatcher.py`から出してここへ移した**（2026-09-07）。outboxの作り直しからも
    同じ規則で選ぶ必要があり、`dispatcher.py`の非公開関数を外から呼ぶ形にすると
    「正本が1つ」という上の約束が名前の上で崩れるため。

    落とすものは2種類。

    1. **スキーマがシートへ同期しない項目**（`properties_synced_to`）。Notionにしか無い
       メタ情報やスキーマ外のプロパティを持ち込まない。`source`に無いキーも飛ばす。
       落ちるのはロールアップ・unique_idのような読み取り専用型だけで、それらは
       `SyncScope.INTERNAL`固定のため最初からこの一覧に入らない（`db_schema/base.py`）。
    2. **リレーション**（`drop_relation_properties`）。書き込み側と同じ関数を使う。
       落とされるものを数に入れると、実際には1列だけの行なのに「補えた」と誤認する
       （2026-09-02、クマ指摘）。

    `exclude`は**プロパティ名の集合**（`Container[str]`）。辞書を渡してもキーで判定
    されるが、型で意図を示しておく（2026-09-03、GeminiのINFO指摘）。
    """
    return drop_relation_properties(
        {
            prop.name: source[prop.name]
            for prop in schema.properties_synced_to(Tool.SPREADSHEET)
            if prop.name not in exclude and prop.name in source
        },
        db_key,
    )
