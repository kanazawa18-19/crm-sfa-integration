"""Notion案件管理DB「AI推奨アクション日」変更を、web-engagement-tool側のGoogle Calendar連携API
へ同期するサービス層。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

from src.calendar_sync.notion_user_email import get_notion_user_email
from src.calendar_sync.plan import build_event_plan
from src.calendar_sync.web_engagement_tool_client import WebEngagementToolCalendarClient


def sync_next_action_date_to_calendar(
    properties: Mapping[str, Any],
    notion_page_id: str,
    *,
    calendar_client: WebEngagementToolCalendarClient | None = None,
) -> dict[str, Any] | None:
    """取得済み案件のカレンダー専用5項目を使い、次回アクションを担当メンバーの
    Google Calendarへ同期する。

    以下の場合は同期をスキップし`None`を返す:
    - アクション内容またはAI推奨アクション日が空欄。**「削除された（Noneになった）場合にカレンダー側の
      予定も削除すべきか」は今回のスコープ外とし、単に同期をスキップすることでよい。**
    - `"担当メンバー"`が無い、または空リスト（誰のカレンダーに登録すればよいか不明なため）。
    - 先頭の担当メンバーIDのメールアドレスが解決できない（`get_notion_user_email`が`None`を
      返した場合）。

    `calendar_client`省略時は`WebEngagementToolCalendarClient()`をデフォルト生成する
    （テストでは注入して差し替える）。

    この関数自体は例外を握りつぶさない（呼び出し元＝webhookハンドラ側で「失敗してもWebhook
    全体は失敗させない」という判断を行う設計とする）。
    """
    plan = build_event_plan(properties, notion_page_id)
    if plan is None:
        logger.info("calendar_sync skipped: reason=missing_action_or_date_or_no_action")
        return None

    reps = properties.get("担当メンバー")
    if not reps:
        logger.info("calendar_sync skipped: reason=missing_rep")
        return None

    rep_email = get_notion_user_email(reps[0])
    if rep_email is None:
        logger.info("calendar_sync skipped: reason=unresolved_rep")
        return None

    if calendar_client is None:
        calendar_client = WebEngagementToolCalendarClient()

    return calendar_client.upsert_event(rep_email=rep_email, **plan)
