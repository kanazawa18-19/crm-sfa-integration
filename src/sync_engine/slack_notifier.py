"""Slack管理チャンネルへのコンフリクト自動解決アラート通知（05_同期・競合制御「アラート通知」）。

重要項目（config/conflict_alert_properties.json）のコンフリクトが自動解決された際、
対象案件・変更項目・採用データ・却下データをSlackへ即時通知する。dispatcherはこの
Protocolを介して通知を行い、テストではモックNotifierを注入できるようにする。
"""

from __future__ import annotations

import os
from typing import Protocol

import requests

from src.sync_engine.conflict_resolver import RejectedData


class SlackNotifier(Protocol):
    """コンフリクト自動解決の通知を送るための最小インターフェース。"""

    def notify_conflict(self, rejected: RejectedData) -> None: ...


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

    def notify_conflict(self, rejected: RejectedData) -> None:
        url = self._url
        if not url:
            # Webhook URL未設定時は通知を送らない（ローカル開発・URL未発行段階での動作を妨げない）。
            return
        text = (
            "[同期コンフリクト自動解決]\n"
            f"対象案件: {rejected.record_id}\n"
            f"変更項目: {rejected.property_name}\n"
            f"採用データ: {rejected.adopted_value}（採用元: {rejected.adopted_tool.value}）\n"
            f"却下データ: {rejected.rejected_value}（却下元: {rejected.rejected_tool.value}）\n"
            f"発生日時: {rejected.occurred_at.isoformat()}"
        )
        requests.post(url, json={"text": text}, timeout=10)
