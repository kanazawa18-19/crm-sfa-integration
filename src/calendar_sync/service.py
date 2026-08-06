"""Notion案件管理DB「次回アクション日」変更を、web-engagement-tool側のGoogle Calendar連携API
へ同期するサービス層。
"""

from __future__ import annotations

from typing import Any, Mapping

from src.calendar_sync.notion_user_email import get_notion_user_email
from src.calendar_sync.web_engagement_tool_client import WebEngagementToolCalendarClient


def sync_next_action_date_to_calendar(
    properties: Mapping[str, Any],
    notion_page_id: str,
    *,
    calendar_client: WebEngagementToolCalendarClient | None = None,
) -> dict[str, Any] | None:
    """`properties`（`SyncEvent.properties`相当の辞書）の「次回アクション日」を担当メンバーの
    Google Calendarへ同期する。

    以下の場合は同期をスキップし`None`を返す:
    - `"次回アクション日"`キーが無い、または値が`None`（次回アクション日が変更されていない
      呼び出し、または削除された呼び出し）。**「削除された（Noneになった）場合にカレンダー側の
      予定も削除すべきか」は今回のスコープ外とし、単に同期をスキップすることでよい。**
    - `"担当メンバー"`が無い、または空リスト（誰のカレンダーに登録すればよいか不明なため）。
    - 先頭の担当メンバーIDのメールアドレスが解決できない（`get_notion_user_email`が`None`を
      返した場合）。

    `calendar_client`省略時は`WebEngagementToolCalendarClient()`をデフォルト生成する
    （テストでは注入して差し替える）。

    この関数自体は例外を握りつぶさない（呼び出し元＝webhookハンドラ側で「失敗してもWebhook
    全体は失敗させない」という判断を行う設計とする）。
    """
    next_action_date = properties.get("次回アクション日")
    if not next_action_date:
        return None
    # Notion側で「時間を含める」がONのページは"2026-08-10T09:00:00+09:00"のような
    # ISO日時文字列になる。web-engagement-tool側のAPIはYYYY-MM-DD形式のみを受け付け
    # 不一致だと400になるため、日付部分のみを取り出す（時刻情報は今回の同期では扱わない）。
    next_action_date = str(next_action_date)[:10]

    reps = properties.get("担当メンバー")
    if not reps:
        return None

    rep_email = get_notion_user_email(reps[0])
    if rep_email is None:
        return None

    if calendar_client is None:
        calendar_client = WebEngagementToolCalendarClient()

    project_name = properties.get("案件名", "（案件名未設定）")
    summary = f"{project_name} - 次回アクション"

    return calendar_client.upsert_event(
        rep_email=rep_email,
        notion_project_id=notion_page_id,
        summary=summary,
        date=next_action_date,
    )
