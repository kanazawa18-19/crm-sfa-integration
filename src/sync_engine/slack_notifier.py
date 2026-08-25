"""Slack管理チャンネルへのアラート通知（05_同期・競合制御「アラート通知」）。

重要項目（config/conflict_alert_properties.json）のコンフリクトが自動解決された際、
対象案件・変更項目・採用データ・却下データをSlackへ即時通知する。dispatcherはこの
Protocolを介して通知を行い、テストではモックNotifierを注入できるようにする。

`notify_new_record_created`/`notify_new_record_issue`は、新規レコード作成
（`AUTO_CREATE_NEW_RECORDS_ENABLED`、2026-08-25、Round2）の運用可視性のために追加した
（shirokuma-sec/obasan-qualityレビューWARN対応）。特に`notify_new_record_issue`は、Notion
ページ作成後にIdMapping登録が失敗した「孤児ページ」の検知（BLOCKER1対応）にも使われる。

■ 例外を投げない設計について（2026-08-25、3回目最終レビューBLOCKER対応）: `WebhookSlackNotifier`
の各`notify_*`メソッドは、`requests.post()`が例外（タイムアウト・接続断・5xx等）を送出しても
**呼び出し元へ一切伝播させない**（内部でtry/exceptし、失敗時はログのみ残して静かに戻る）。
`Dispatcher._handle_uncertain_notion_page_creation()`/`_handle_orphaned_notion_page()`のような
「他の保護ロジックが失敗した後の最終防衛線」でこの通知を呼んでいる箇所があり、もし通知自体が
例外を投げると、その例外がWebhookハンドラの広い`except Exception`まで伝播して500応答となり、
kintone/Zoho側のリトライで同じイベントが再送され、Round2全体が防ごうとしていた重複ページ
作成が（保護ロジックそのものは正しく動いたにもかかわらず）通知処理の失敗だけを引き金に
再現してしまう。呼び出し箇所ごとに個別のtry/exceptを重ねるのではなく、Notifier自体を
「絶対に失敗しない」実装にすることで、全ての呼び出し箇所を一括で守る。
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import requests

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import RejectedData

logger = logging.getLogger(__name__)


class SlackNotifier(Protocol):
    """アラート通知を送るための最小インターフェース。"""

    def notify_conflict(self, rejected: RejectedData) -> None: ...

    def notify_new_record_created(
        self, *, db_key: str, source_tool: Tool, external_id: str, notion_page_id: str
    ) -> None:
        """新規Notionページが作成されたことを通知する。"""
        ...

    def notify_new_record_issue(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
        notion_page_id: str | None = None,
    ) -> None:
        """新規レコード作成処理で問題が発生したことを通知する（必須プロパティ不足による
        スキップ、IdMapping登録失敗による孤児ページ発生等）。`notion_page_id`は、Notion
        ページ自体は既に作成されている場合（孤児ページ）にのみ指定される。
        """
        ...


class WebhookSlackNotifier:
    """SLACK_WEBHOOK_URL_ALERT環境変数のIncoming WebhookへシンプルにHTTP POSTする実装。

    本番投入時はリトライ・レート制限・Block Kit等によるリッチな整形を検討すること。
    ここでは仕様書05節の通知内容（対象案件 / 変更項目 / 採用データ / 却下データ）を
    満たす最低限のテキスト通知のみを実装する。
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url

    @property
    def _url(self) -> str | None:
        return self._webhook_url or os.environ.get("SLACK_WEBHOOK_URL_ALERT")

    def _post(self, text: str) -> None:
        """Slack Incoming WebhookへPOSTする。`requests.post()`が送出する例外
        （タイムアウト・接続断・5xx等）は、呼び出し元の同期処理（`Dispatcher`の保護ロジック
        自体を含む）を巻き込んで失敗させないよう、ここで捕捉してログに残すのみとする
        （モジュールdocstring「例外を投げない設計について」参照。3回目最終レビューBLOCKER
        対応、2026-08-25）。Slack通知自体はあくまで副次的な機能であり、本来の同期処理を
        失敗させてはならない（`src/audit_log/recorder.py`の「副次機能は失敗してもメインを
        止めない」方針と同じ考え方）。
        """
        url = self._url
        if not url:
            # Webhook URL未設定時は通知を送らない（ローカル開発・URL未発行段階での動作を妨げない）。
            return
        try:
            requests.post(url, json={"text": text}, timeout=10)
        except Exception:
            logger.warning(
                "WebhookSlackNotifier: failed to post to Slack webhook; continuing without "
                "raising (Slack notification is a secondary feature and must not block the "
                "caller's main processing)",
                exc_info=True,
            )

    def notify_conflict(self, rejected: RejectedData) -> None:
        text = (
            "[同期コンフリクト自動解決]\n"
            f"対象案件: {rejected.record_id}\n"
            f"変更項目: {rejected.property_name}\n"
            f"採用データ: {rejected.adopted_value}（採用元: {rejected.adopted_tool.value}）\n"
            f"却下データ: {rejected.rejected_value}（却下元: {rejected.rejected_tool.value}）\n"
            f"発生日時: {rejected.occurred_at.isoformat()}"
        )
        self._post(text)

    def notify_new_record_created(
        self, *, db_key: str, source_tool: Tool, external_id: str, notion_page_id: str
    ) -> None:
        text = (
            "[新規レコード自動作成]\n"
            f"DB: {db_key}\n"
            f"作成元: {source_tool.value}（external_id={external_id}）\n"
            f"Notion page ID: {notion_page_id}"
        )
        self._post(text)

    def notify_new_record_issue(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
        notion_page_id: str | None = None,
    ) -> None:
        lines = [
            "[新規レコード自動作成で問題が発生しました]",
            f"DB: {db_key}",
            f"対象: {source_tool.value}（external_id={external_id}）",
            f"理由: {reason}",
            f"詳細: {detail}",
        ]
        if notion_page_id:
            lines.append(f"⚠️ Notion page ID（要確認・孤児ページの可能性あり）: {notion_page_id}")
        self._post("\n".join(lines))
