"""他ツール(kintone/Zoho等)から受け取った自由入力の会社名テキストを、Notionの各種
リレーション先ページIDへ解決する(2026-08-25)。

`ClientNameIndex`（Notion取引先マスターDBのローカルミラー、`src/relation_sync/db.py`）を
検索するだけのため、Notion APIを呼ばずWebhookの同期応答時間内に解決できる。曖昧な場合
（候補0件・複数件）は自動確定せず`RelationReviewQueue`へ記録し、呼び出し元には`None`を
返す（呼び出し元は`None`の場合、そのリレーションを更新しない＝既存の値を上書きしない）。

正規化には`src.migration.zoho_client_master.normalize_company_name_strong()`を使う
（全角/半角統一・法人格表記ゆれ吸収。`src/migration/notion_dedupe.py`の名寄せロジックも
同じ関数をimportして使っており、二重実装を避けるためそちらを再利用する）。
"""

from __future__ import annotations

import logging
import os

from src.migration.zoho_client_master import normalize_company_name_strong
from src.relation_sync.db import find_by_normalized_name
from src.relation_sync.review_queue import enqueue_for_review

logger = logging.getLogger(__name__)

# `src/relation_sync/`の書き込み系（Webhook反映・夜間reconciliation）は
# `PROJECT_MIRROR_SYNC_ENABLED`と同じ「未設定なら無効化」パターンで、production_wiring.py
# 側（呼び出し元）がenv varを見て呼ぶかどうかを判断する。しかしこの関数はその設計とは異なり、
# `src/sync_engine/webhook_handlers/kintone_field_transforms.py`の`KINTONE_FIELD_TRANSFORMS`
# テーブルから常時（フラグ無しで）呼ばれる同期応答経路に組み込まれている。ロールアウトの
# 「インフラ整備のみでは本番挙動は変わらない」前提を保つため、書き込み系と同じ
# `RELATION_SYNC_ENABLED`をこの関数自身でも確認し、無効時は`ClientNameIndex`への問い合わせも
# `RelationReviewQueue`への記録も行わずNoneを返す（true resolverと同じ「未確定」応答だが、
# 副作用が一切発生しない点が異なる）。無効時にClientNameIndexが空のまま検索し続けると、
# 正しい取引先名であっても常に「候補なし」と判定され、レビューキューへ無意味なエントリが
# 積み上がってしまうため。
_RELATION_SYNC_ENV_VAR = "RELATION_SYNC_ENABLED"


def resolve_client_master_relation(
    raw_name: str, *, source_tool: str, source_record_id: str
) -> str | None:
    """取引先マスターDBへのリレーション先Notion page IDを解決する。

    `raw_name`（前後空白等を含みうる自由入力の会社名）を`normalize_company_name_strong()`
    で正規化し、`ClientNameIndex`を完全一致検索する。1件だけヒットした場合のみそのpage IDを
    返す。

    `RELATION_SYNC_ENABLED`環境変数が`"true"`でない場合は常に`None`を返す（`ClientNameIndex`
    への問い合わせ・`RelationReviewQueue`への記録のいずれも行わない、上記モジュールコメント
    参照）。

    空文字列・空白のみの`raw_name`は「未入力」であり解決を試みる対象ではないため、
    レビューキューへの記録もせずNoneを返す（kintoneのclient_nameは自由入力欄であり、
    空欄のアクション履歴レコードごとにレビューキューへ無意味なエントリが積み上がるのを
    避けるため）。
    """
    if os.environ.get(_RELATION_SYNC_ENV_VAR, "").strip().lower() != "true":
        return None
    if not raw_name or not raw_name.strip():
        return None

    normalized = normalize_company_name_strong(raw_name)
    matches = find_by_normalized_name(normalized)

    if len(matches) == 1:
        return matches[0]["notion_page_id"]

    reason = "no matching client found" if not matches else f"ambiguous: {len(matches)} candidates"
    logger.info(
        "resolve_client_master_relation: 解決できなかったためレビューキューへ記録します"
        " (raw_name=%r, source_tool=%r, source_record_id=%r, reason=%s)",
        raw_name,
        source_tool,
        source_record_id,
        reason,
    )
    enqueue_for_review(
        source_tool=source_tool,
        source_record_id=source_record_id,
        target_db_key="client_master",
        raw_value=raw_name,
        candidate_notion_page_ids=[m["notion_page_id"] for m in matches],
        # candidateNotionPageIdsと同じ順序で対になる各候補の取引先名（shirokuma-sec/
        # obasan-qualityレビューWARN対応、2026-08-25。raw_nameを捨てずに保持しないと、
        # scripts/list_relation_review_queue.pyの出力がpage IDの羅列になり、運用者が
        # 曖昧一致を判断するために毎回Notionを開く必要が生じてしまうため）。
        candidate_raw_names=[m["raw_name"] for m in matches],
    )
    return None
