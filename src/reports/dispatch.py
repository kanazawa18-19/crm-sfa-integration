"""日報・週報の配信先抽象化（07_日報週報仕様「配信先はSlack/Teams/Chatwork＋Notionダッシュボード」）。

レポートの生成（`daily_report.py` / `weekly_report.py`によるテキスト組み立て）と配信
（本モジュール）を分離する。`src.sync_engine.slack_notifier.SlackNotifier`と同様、
Protocolを介して配信を行い、テストではモックNotifierを注入できるようにする。

Notionダッシュボードの更新は本モジュールのスコープ外（別途Notion API連携で実装すること）。
"""

from __future__ import annotations

import os
from typing import Protocol

import requests


class ReportNotifier(Protocol):
    """生成済みレポートテキストを配信するための最小インターフェース。"""

    def send_report(self, text: str) -> None: ...


class WebhookSlackReportNotifier:
    """SLACK_WEBHOOK_URL_REPORT環境変数のIncoming WebhookへシンプルにHTTP POSTする実装。

    `src.sync_engine.slack_notifier.WebhookSlackNotifier`と同様のパターン
    （Webhook URL未設定時は送信をスキップし、ローカル開発・URL未発行段階での動作を妨げない）。
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url

    @property
    def _url(self) -> str | None:
        return self._webhook_url or os.environ.get("SLACK_WEBHOOK_URL_REPORT")

    def send_report(self, text: str) -> None:
        url = self._url
        if not url:
            return
        requests.post(url, json={"text": text}, timeout=10)


class TeamsReportNotifier:
    """Microsoft Teams向けの配信スタブ。

    TODO: Teams Incoming Webhook（Adaptive Card等）でのレポート配信を実装する。
    現時点ではTeams側の配信フォーマットが未確定のため、`ReportNotifier`を満たす
    プレースホルダのみ用意している。

    運用ルール: `WebhookSlackReportNotifier`と異なり、本Notifierは未設定時に
    黙ってスキップする実装にはなっておらず、`send_report`を呼ぶと必ず
    `NotImplementedError`になる。そのため呼び出し側（配信バッチ等）は、
    `ENABLE_TEAMS_REPORT`のような機能フラグが立っていない限り、本Notifierを
    そもそも配信対象のNotifier一覧に含めないこと。
    """

    def send_report(self, text: str) -> None:
        raise NotImplementedError("Teams配信は未実装（TODO: Teams Webhook連携）")


class ChatworkReportNotifier:
    """Chatwork向けの配信スタブ。

    TODO: Chatwork API（メッセージ送信エンドポイント）でのレポート配信を実装する。

    運用ルール: `WebhookSlackReportNotifier`と異なり、本Notifierは未設定時に
    黙ってスキップする実装にはなっておらず、`send_report`を呼ぶと必ず
    `NotImplementedError`になる。そのため呼び出し側（配信バッチ等）は、
    `ENABLE_CHATWORK_REPORT`のような機能フラグが立っていない限り、本Notifierを
    そもそも配信対象のNotifier一覧に含めないこと。
    """

    def send_report(self, text: str) -> None:
        raise NotImplementedError("Chatwork配信は未実装（TODO: Chatwork API連携）")
