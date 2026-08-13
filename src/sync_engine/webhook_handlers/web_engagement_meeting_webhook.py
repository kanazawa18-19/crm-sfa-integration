"""web-engagement-tool Webhookの受信ハンドラ（Googleカレンダー商談イベント通知の受信）。

web-engagement-tool側（別リポジトリ）が営業担当のGoogleカレンダーから検知した予定
（参加者に社外メールを含むもの）を、`X-Webhook-Secret`ヘッダー付きでfire-and-forgetで
プッシュしてくる想定（呼び出し元は範囲外）。

`web_engagement_webhook.py`と同じ設計方針: `Dispatcher`/`IdMappingStore`は経由しない
（新規レコード作成は`Dispatcher.dispatch()`のスコープ外、`dispatcher.py`のコメント参照）。
参加者メールから対象案件を1件に絞り込めた場合のみ、Slackへ承認依頼を投稿する
（`src/meeting_sync/slack_approval.py`）。この時点ではまだNotionへ書き込まない
（Notionへの実際の書き込みは承認ボタン押下時、`slack_interaction_webhook.py`が行う）。

承認依頼はSlackの共有チャンネルではなく、`rep_email`（そのカレンダー予定の持ち主）本人へ
DMで送る（`src/meeting_sync/slack_approval.py`のdocstring参照）。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）:
{
  "google_event_id": "abc123@google.com",
  "title": "【商談（訪問）】〇〇ホテル様",
  "starts_at": "2026-08-12T10:00:00+09:00",
  "attendee_emails": ["yamada@example.com", "sales@cnctor.jp"],
  "meet_link": "https://meet.google.com/xxx-xxxx-xxx",
  "rep_email": "sales@cnctor.jp",
  "document_url": "https://docs.google.com/document/d/xxxx"
}

`document_url`はGeminiの議事録・録画リンク（省略可、無ければNone）。承認時にNotionの
「議事録・録画リンク」プロパティへそのまま書き込む（`src/meeting_sync/slack_approval.py`の
`_build_action_properties()`参照）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from src.db_schema.registry import get_schema
from src.meeting_sync.action_type import infer_action_type
from src.meeting_sync.matcher import find_matching_project
from src.meeting_sync.slack_approval import MeetingCandidate, post_approval_request
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    internal_error_response,
    logger,
    unauthorized_response,
    verify_webhook_secret,
)

_CONTACT_DB_KEY = "contact"
_PROJECT_DB_KEY = "project"
_PROJECT_TITLE_PROPERTY = "案件名"


def _default_contact_client() -> HttpNotionClient:
    schema = get_schema(_CONTACT_DB_KEY)
    return HttpNotionClient(_CONTACT_DB_KEY, schema.notion_database_id)


def _default_project_client() -> HttpNotionClient:
    schema = get_schema(_PROJECT_DB_KEY)
    return HttpNotionClient(_PROJECT_DB_KEY, schema.notion_database_id)


def _internal_domains() -> frozenset[str]:
    raw = os.environ.get("INTERNAL_EMAIL_DOMAINS", "")
    return frozenset(
        domain.strip().lower() for domain in raw.split(",") if domain.strip()
    )


def _project_display_name(project_client: HttpNotionClient, project_page_id: str) -> str:
    page = project_client.get_raw_page(project_page_id)
    props = page.get("properties") or {}
    title_prop = props.get(_PROJECT_TITLE_PROPERTY)
    if title_prop is None:
        return project_page_id
    parts = title_prop.get("title") or []
    text = "".join(part.get("plain_text", "") for part in parts)
    return text or project_page_id


def _attendee_display(attendee_emails: list[str], internal_domains: frozenset[str]) -> str:
    external = [
        email
        for email in attendee_emails
        if email and email.strip().lower().rsplit("@", 1)[-1] not in internal_domains
    ]
    return "、".join(external) if external else "(不明)"


def handler(
    event: Mapping[str, Any],
    context: object,
    *,
    contact_client: HttpNotionClient | None = None,
    project_client: HttpNotionClient | None = None,
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。

    `contact_client`/`project_client`未注入時は環境変数から本番用の`HttpNotionClient`を
    構築する（テスト時のみ注入を想定）。
    """
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "WEB_ENGAGEMENT_MEETING_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
        google_event_id = payload.get("google_event_id")
        title = payload.get("title")
        starts_at = payload.get("starts_at")
        attendee_emails = payload.get("attendee_emails")
        if not isinstance(google_event_id, str) or not google_event_id.strip():
            raise ValueError("payload.google_event_id is required and must be a non-empty string")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("payload.title is required and must be a non-empty string")
        if not isinstance(starts_at, str) or not starts_at.strip():
            raise ValueError("payload.starts_at is required and must be a non-empty string")
        if not isinstance(attendee_emails, list) or not attendee_emails:
            raise ValueError("payload.attendee_emails is required and must be a non-empty list")
        rep_email = payload.get("rep_email")
        if not isinstance(rep_email, str) or not rep_email.strip():
            raise ValueError("payload.rep_email is required and must be a non-empty string")
        meet_link = payload.get("meet_link")
        document_url = payload.get("document_url")
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except ValueError as exc:
        return bad_request_response(str(exc))

    try:
        internal_domains = _internal_domains()
        contact = contact_client if contact_client is not None else _default_contact_client()
        project = project_client if project_client is not None else _default_project_client()

        project_page_id = find_matching_project(
            attendee_emails,
            contact,
            project,
            internal_domains=internal_domains,
            event_id=google_event_id.strip(),
        )
        if project_page_id is None:
            return {
                "statusCode": 200,
                "body": json.dumps({"posted": False, "reason": "no_unique_project_match"}),
            }

        candidate = MeetingCandidate(
            event_id=google_event_id.strip(),
            title=title.strip(),
            action_type=infer_action_type(title, has_meet_link=bool(meet_link)),
            action_date=starts_at[:10],
            project_page_id=project_page_id,
            project_name=_project_display_name(project, project_page_id),
            attendee_display=_attendee_display(attendee_emails, internal_domains),
            rep_email=rep_email.strip(),
            # DM送信先解決時にpost_approval_request()内で実際の値へ差し替えられる
            # （src/meeting_sync/slack_approval.pyのMeetingCandidate docstring参照）。
            rep_slack_user_id="",
            document_url=document_url if isinstance(document_url, str) and document_url.strip() else None,
        )
        # obasan-qualityレビューBLOCKER対応（2026-08-13）: 以前は呼んだだけで結果を
        # 見ずに`posted: True`を返していた（Slack送信が実際に失敗しても気づけなかった）。
        # 戻り値をそのままレスポンスへ反映する。
        posted = post_approval_request(candidate)
    except Exception:
        logger.exception(
            "unexpected error while matching web-engagement-tool calendar event to a project"
        )
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"posted": posted, "project_id": project_page_id}),
    }
