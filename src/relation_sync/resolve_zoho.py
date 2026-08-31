"""Zohoアクション履歴モジュール(CustomModule2)の取引先マスターリレーション解決(2026-08-25、Round2)。

`resolve.py`の`resolve_client_master_relation()`（自由入力の会社名テキストをClientNameIndexで
名寄せする）とは別に、Zoho側は移行時にNotionページへの直リンクが埋め込まれた列を持つ
（`src/migration/_utils.py`の`extract_notion_page_id()`参照。事前調査で確認済み）。

config/zoho_field_mapping.json（CustomModule2セクション）で確認済みの実フィールドapi_name:
- `field22`（ラベル「【Notion】取引先マスター」）: `会社名 (NotionページURL)`形式でNotionページ
  への直リンクが埋め込まれていることがある自由記述テキスト。
- `field6`（ラベル「取引先」）: Zoho側の生の会社名（自由入力）。埋め込みヒントを持たない
  新規Zohoレコード（移行後に作成されたレコード等）向けのフォールバック。

優先順位（金沢さん指示）:
1. `field22`に埋め込みNotionページIDヒントがあれば、それをそのまま使う（ID直接参照なので
   名寄せ不要、最も信頼性が高い）。
2. 埋め込みヒントが無ければ、`field6`の生の会社名を`resolve_client_master_relation()`へ渡し、
   ClientNameIndexでの名寄せ解決を試みる（完全一致のみ自動、曖昧なら`RelationReviewQueue`へ）。

Zoho Webhook（Notification API）が届けるのは変更されたフィールドのみのdelta
（`affected_values[*].values`）であり、`field22`/`field6`の一方だけが変更された場合、
判定に必要なもう一方の現在値はペイロードに含まれない。そのため`zoho_client`
（`get_record(module, record_id)`を持つ最小Protocol）を注入した場合のみ、Zoho CRM APIで
レコード全体を取得し不足分を補う（未注入・API呼び出し失敗時は、取得できなかった側は
「値なし」として扱い、安全側に倒す）。

「案件」(project)リレーションは今回も意図的にスコープ外とする。CustomModule2には
「【Notion】案件」のような埋め込みNotionページIDヒントを持つフィールドが存在せず（事前調査
で確認済み）、`field11`（案件名）・`field8`（案件）は自由記述の案件名文字列でしかないため、
kintone側（`kintone_field_transforms.py`のモジュールdocstring参照）と同じ理由
（自動選択はもちろん、レビューキューへ積んでも人間が判断できる材料が無い）でスコープ外とする。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol

from src.db_schema.base import Tool
from src.migration._utils import extract_notion_page_id
from src.relation_sync.resolve import resolve_client_master_relation
from src.relation_sync.review_queue import enqueue_for_review
from src.sync_engine.id_mapping import IdMappingStore

logger = logging.getLogger(__name__)

# resolve.pyの_RELATION_SYNC_ENV_VARと同じ環境変数（Round1と同じ既存フラグを流用。新規フラグは
# 追加しない、金沢さん指示）。resolve_client_master_relation()自身も同フラグを見るが、本モジュール
# の埋め込みヒント経路(1)はresolve_client_master_relation()を経由しないため、ここでも独立して
# 確認する（無効時にZoho APIへの問い合わせ自体を行わないようにするため）。
_RELATION_SYNC_ENV_VAR = "RELATION_SYNC_ENABLED"

# config/zoho_field_mapping.jsonのCustomModule2セクションで確認済みの実api_name
# （モジュールdocstring参照）。
_NOTION_CLIENT_MASTER_HINT_FIELD = "field22"  # ラベル:「【Notion】取引先マスター」
_RAW_CLIENT_NAME_FIELD = "field6"  # ラベル:「取引先」
_ACTION_MODULE = "CustomModule2"


class ZohoActionRecordClient(Protocol):
    """`resolve_zoho_action_client_master_relation`が要求するZohoクライアントの最小
    インターフェース（`src.sync_engine.clients.zoho_client.HttpZohoClient.get_record`が実装）。
    """

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None: ...


def extract_zoho_lookup_name(value: Any) -> str:
    """Zohoのルックアップ項目から会社名を取り出す。

    **ルックアップ項目の値は`{"name": "◯◯", "id": "..."}`という辞書で返る。**
    これをそのまま`str()`すると`"{'name': '◯◯', 'id': '...'}"`という文字列になり、
    名寄せが必ず失敗する（2026-08-31、本番ログで発覚。
    `resolve_client_master_relation: 解決できなかったためレビューキューへ記録します
    (raw_name="{'name': 'ホテルユクエスタ旭橋', 'id': '...'}")`）。
    **Zoho発のアクションの取引先リレーションは、この不具合で一度も解決できていなかった。**

    文字列で来た場合はそのまま返す（Webhookのdeltaでは文字列で来ることもある）。
    """
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    return str(value) if value is not None else ""


def resolve_zoho_action_client_master_relation(
    *,
    record_id: str,
    changed_values: Mapping[str, Any],
    zoho_client: ZohoActionRecordClient | None,
) -> str | None:
    """⑥アクション履歴DBの「👨‍👩‍👧‍👦 取引先マスター」リレーション先Notion page IDを解決する。

    `changed_values`はZoho Webhookのdelta（`affected_values[*].values`、api_name→値）。
    `field22`/`field6`のいずれかがそこに含まれていればその値をそのまま使い、含まれていない
    （=今回のWebhook通知では変更されていない）側は`zoho_client`（省略可）でレコード全体を
    取得して現在値を補う。

    `RELATION_SYNC_ENABLED`環境変数が`"true"`でない場合は常に`None`を返す（Zoho API呼び出しも
    `resolve_client_master_relation()`（ひいてはClientNameIndexへの問い合わせ・
    RelationReviewQueueへの記録）も一切行わない。Round1のresolve.py同様、インフラ整備段階では
    本番挙動を変えないため）。
    """
    if os.environ.get(_RELATION_SYNC_ENV_VAR, "").strip().lower() != "true":
        return None

    fetched_record: dict[str, Any] | None = None

    def _fetch_record_once() -> dict[str, Any]:
        nonlocal fetched_record
        if fetched_record is not None:
            return fetched_record
        if zoho_client is None:
            fetched_record = {}
            return fetched_record
        try:
            fetched_record = zoho_client.get_record(_ACTION_MODULE, record_id) or {}
        except Exception:
            logger.warning(
                "resolve_zoho_action_client_master_relation: 取引先マスターリレーション解決の"
                "ためZoho CustomModule2レコードの現在値取得に失敗しました。埋め込みヒント・"
                "生の会社名のいずれも取得できなかった側は「値なし」として扱います"
                " (record_id=%r)",
                record_id,
                exc_info=True,
            )
            fetched_record = {}
        return fetched_record

    if _NOTION_CLIENT_MASTER_HINT_FIELD in changed_values:
        hint_source: Any = changed_values.get(_NOTION_CLIENT_MASTER_HINT_FIELD)
    else:
        hint_source = _fetch_record_once().get(_NOTION_CLIENT_MASTER_HINT_FIELD)

    embedded_hint = extract_notion_page_id(hint_source if isinstance(hint_source, str) else None)
    if embedded_hint:
        return embedded_hint

    if _RAW_CLIENT_NAME_FIELD in changed_values:
        raw_name = changed_values.get(_RAW_CLIENT_NAME_FIELD)
    else:
        raw_name = _fetch_record_once().get(_RAW_CLIENT_NAME_FIELD)

    return resolve_client_master_relation(
        extract_zoho_lookup_name(raw_name),
        source_tool="zoho",
        source_record_id=record_id,
    )


def resolve_zoho_relation_by_lookup_id(
    value: Any,
    *,
    target_db_key: str,
    property_name: str,
    id_mapping_store: IdMappingStore | None,
    source_record_id: str,
) -> str | None:
    """Zohoのルックアップ項目から、**名寄せせずに**リレーション先Notion page IDを解決する。

    ルックアップの値は`{"name": "◯◯", "id": "22334000..."}`で、この`id`は**相手モジュールの
    Zohoレコードid**。同じレコードは既にNotionへ同期されていればIdMappingを持っているので、
    `find_by_external_id()`で引けば会社名の突き合わせを一切せずに確定できる。

    取引先マスターの解決（`resolve_client_master_relation()`）が自由入力テキストの名寄せに
    頼っているのは、kintone側の`client_name`が単なる文字列だから。ルックアップ項目には
    idが入っているので、こちらの経路の方が原理的に正確で速い。

    実測（2026-08-31、各モジュール200件）で、以下は実際に値が入っていることを確認済み:
      Deals  取引先名(Account_Name) 200/200 / 取引先担当者(field10) 195/200
             提案サービス1(field72) 194/200
      Products 案件(field12) 10/200
      CustomModule2 チェーン(field1) 6/200
      CustomModule3 連絡先(field10) 2/200
    一方、CustomModule2の案件(field8)・案件名(field11)・連絡先(field5)は**200件すべて空**で、
    Zoho側にデータが無い。アクション→案件のリレーションは、実装しても解決するものが無い。

    解決できない主因は「相手のレコードがまだNotionへ同期されていない」こと。推測で別の
    ページに紐付けると静かな誤紐付けになるため、Noneを返して`RelationReviewQueue`へ積む。
    """
    if os.environ.get(_RELATION_SYNC_ENV_VAR, "").strip().lower() != "true":
        return None
    if not isinstance(value, Mapping):
        return None
    target_zoho_id = value.get("id")
    if not target_zoho_id:
        return None
    if id_mapping_store is None:
        logger.info(
            "resolve_zoho_relation_by_lookup_id: IdMappingStoreが渡されていないため解決できません"
            " (property=%r, source_record_id=%r)",
            property_name,
            source_record_id,
        )
        return None
    try:
        mapping = id_mapping_store.find_by_external_id(
            Tool.ZOHO, str(target_zoho_id), db_key=target_db_key
        )
    except Exception:
        logger.warning(
            "resolve_zoho_relation_by_lookup_id: IdMappingの逆引きに失敗しました"
            " (property=%r, target_db_key=%r, target_zoho_id=%r)",
            property_name,
            target_db_key,
            target_zoho_id,
            exc_info=True,
        )
        return None
    if mapping is not None:
        return mapping.notion_key

    raw_name = extract_zoho_lookup_name(value)
    logger.info(
        "resolve_zoho_relation_by_lookup_id: 相手レコードのIdMappingが無いため解決できません"
        "（Notionへ未同期の可能性）。レビューキューへ記録します"
        " (property=%r, target_db_key=%r, target_zoho_id=%r, raw_name=%r)",
        property_name,
        target_db_key,
        target_zoho_id,
        raw_name,
    )
    try:
        enqueue_for_review(
            source_tool="zoho",
            source_record_id=source_record_id,
            target_db_key=target_db_key,
            raw_value=raw_name or str(target_zoho_id),
            # 候補は無い（曖昧なのではなく、相手がまだNotionに存在しない）。
            candidate_notion_page_ids=[],
            candidate_raw_names=[],
        )
    except Exception:
        logger.warning(
            "resolve_zoho_relation_by_lookup_id: レビューキューへの記録に失敗しました",
            exc_info=True,
        )
    return None
