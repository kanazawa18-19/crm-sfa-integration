"""取引先マスターリレーションの「後勝ち」上書き防止ガード（複数Webhookハンドラ共通、2026-08-25）。

kintone Webhook（`kintone_webhook.py`）向けに実装した仕組み（GPT-5.6クロスレビュー指摘対応、
2026-08-25）を、Zoho Webhook（`zoho_webhook.py`）でも同じ設計思想でそのまま使うために
ツール非依存の形へ切り出したもの。

自動解決した「👨‍👩‍👧‍👦 取引先マスター」を素朴に毎回書き込むと、Notion上で人が手動修正した
リレーションを、後日ソース側（kintoneの`client_name`・Zohoの`field6`/`field22`）が再編集
されるたびに黙って上書きしてしまう（この機能が防ごうとしている「静かな誤紐付け」そのものを
引き起こす）。そのため`id_mapping_store`/`notion_client`（いずれも省略可）を注入した場合のみ、
対応するNotionページの現在の「👨‍👩‍👧‍👦 取引先マスター」プロパティを読み、**既に何か値が
設定されていれば自動解決の結果があってもそのプロパティへの書き込みを行わない**
（`drop_client_master_relation_if_already_set`参照。自動反映は「Notion側がまだ未設定の場合の
み」に限定する）。現在値の確認自体に失敗した場合も、安全側に倒して書き込みをスキップする
（既存のNotion側の状態が不明なまま上書きするリスクを避けるため）。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.id_mapping import IdMappingStore

logger = logging.getLogger(__name__)

# ⑥アクション管理の「取引先マスター」リレーション（kintone_field_transforms.py/
# zoho_field_transforms.py参照）。両ハンドラで共通のNotionプロパティ名。
CLIENT_MASTER_RELATION_PROPERTY = "👨‍👩‍👧‍👦 取引先マスター"


class NotionRelationLookupClient(Protocol):
    """`drop_client_master_relation_if_already_set`が要求するNotionクライアントの
    最小インターフェース（`src.sync_engine.clients.notion_client.HttpNotionClient.get_page`
    が実装）。"""

    def get_page(self, page_id: str) -> dict[str, Any] | None: ...


def drop_client_master_relation_if_already_set(
    properties: dict[str, Any],
    *,
    tool: Tool,
    record_id: str,
    db_key: str,
    id_mapping_store: IdMappingStore,
    notion_client: NotionRelationLookupClient,
    property_name: str = CLIENT_MASTER_RELATION_PROPERTY,
) -> None:
    """`properties`に含まれる自動解決済みの取引先マスターリレーションを、対応するNotion
    ページに既に何か値が設定されている場合は取り除く（`properties`を直接書き換える）。

    現在値の確認自体（IdMappingStoreの逆引き・Notion APIのページ取得）に失敗した場合も、
    安全側に倒してこのプロパティへの書き込みをスキップする（既存のNotion側の状態が不明な
    まま上書きするリスクを避けるため。`src/migration/notion_dedupe.py`のneeds_review方針
    「確信が持てないケースは自動で書き込まない」と同じ考え方）。
    """
    try:
        mapping = id_mapping_store.find_by_external_id(tool, record_id, db_key=db_key)
        if mapping is None:
            # まだ移行されていない（対応するNotionページ自体が存在しない）レコード。
            # 上書きの心配が無いため、自動解決した値をそのまま通す。
            return
        current = notion_client.get_page(mapping.notion_key)
    except Exception:
        logger.warning(
            "%s webhook: failed to check current client-master relation before writing; "
            "skipping this property to avoid silently overwriting an existing value "
            "(record_id=%r)",
            tool.value,
            record_id,
            exc_info=True,
        )
        properties.pop(property_name, None)
        return

    if current is None:
        # ページが見つからない（削除済み等）。dispatcher側の通常の書き込み処理に委ねる。
        return
    if current.get(property_name):
        logger.info(
            "%s webhook: client-master relation is already set on the Notion page; "
            "not overwriting with the auto-resolved value (record_id=%r, notion_page_id=%r)",
            tool.value,
            record_id,
            mapping.notion_key,
        )
        properties.pop(property_name, None)
