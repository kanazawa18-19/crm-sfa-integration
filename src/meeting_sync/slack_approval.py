"""Slack Bot Tokenでの承認依頼投稿・ボタン押下(interactivity)への応答。

`src/sync_engine/slack_notifier.py`（Incoming Webhookでの一方向通知）とは別物。ボタンの
コールバックを受けるには`chat.postMessage`（Bot Token）でメッセージを送る必要があるため、
本モジュール専用に`SLACK_BOT_TOKEN`を新規に導入する（新規SDK依存は増やさず、
`slack_notifier.py`と同様に`requests`で直接Slack Web APIを呼ぶ）。

承認依頼は共有チャンネルではなく、対象のGoogleカレンダー予定の担当営業本人へ**DM**で送る
（2026-08-13、金沢さん要望）。`users.lookupByEmail`でメールアドレスからSlackユーザーIDを
解決し、`conversations.open`でDMチャンネルを開いてから`chat.postMessage`する。これにより
「誰でも承認ボタンを押せてしまう」リスクも自然に解消される（担当営業本人にしか届かないため）。
Bot Tokenには`chat:write`に加えて`users:read.email`・`im:write`スコープが必要。

状態の持たせ方: 新しいDBテーブルは作らず、Slackボタンの`value`にNotion作成に必要な
最小限のフィールドをJSON文字列として埋め込む（`MeetingCandidate.to_button_value()`/
`from_button_value()`）。承認ボタン押下時にそのJSONをパースしてNotionへ書き込む。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Protocol

import requests

from src.sync_engine.clients.notion_lookup import find_page_id_by_text_property

logger = logging.getLogger(__name__)

_SLACK_API_BASE = "https://slack.com/api"
_REQUEST_TIMEOUT_SECONDS = 10

_EVENT_ID_PROPERTY = "Googleカレンダーイベントid"
_TITLE_PROPERTY = "商談回数・電話回数・メール回数（何回目）"
_ACTION_TYPE_PROPERTY = "アクション種別"
_ACTION_DATE_PROPERTY = "アクション日"
_MEMO_PROPERTY = "履歴メモ"
_PROJECT_PROPERTY = "案件名"
_CONTACT_PERSON_PROPERTY = "先方担当者"

APPROVE_ACTION_ID = "approve_meeting_action"
REJECT_ACTION_ID = "reject_meeting_action"

# shirokuma-secレビューWARN対応（2026-08-13）: SlackボタンのvalueはBlock Kit APIの仕様上
# 最大2000文字。参加者が多い商談・タイトルが長い商談でこれを超えると`chat.postMessage`が
# invalid_blocksエラーで失敗し、大型商談ほどSlack通知自体が飛ばないという逆説的な事故に
# なりうるため、承認処理に必須ではない表示用フィールド（タイトル・参加者表示）だけを
# 切り詰める。
_MAX_BUTTON_VALUE_LEN = 2000
_MAX_TITLE_LEN = 150
_MAX_ATTENDEE_DISPLAY_LEN = 300


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


class ActionNotionClient(Protocol):
    """本モジュールがアクション履歴DBクライアントに要求する最小インターフェース。"""

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def create_page(self, properties: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class MeetingCandidate:
    """Slackボタンの`value`に埋め込む、Notion作成に必要な最小限のフィールド。"""

    event_id: str
    title: str
    action_type: str
    action_date: str  # YYYY-MM-DD
    project_page_id: str
    project_name: str
    attendee_display: str
    rep_email: str
    # shirokuma-secレビューWARN対応（2026-08-13）: 承認ボタンを押したSlackユーザーが、
    # DM送信先として解決した本人と一致するかの多層防御に使う（DMの機密性のみに依存しない）。
    # `_resolve_dm_channel()`でメールアドレス解決時に得たIDをそのまま埋め込む。
    rep_slack_user_id: str

    def to_button_value(self) -> str:
        payload = asdict(self)
        payload["title"] = _truncate(self.title, _MAX_TITLE_LEN)
        payload["attendee_display"] = _truncate(self.attendee_display, _MAX_ATTENDEE_DISPLAY_LEN)
        value = json.dumps(payload, ensure_ascii=False)
        if len(value) > _MAX_BUTTON_VALUE_LEN:
            # 切り詰め後もなお超える場合（参加者表示が非常に長い等）は、承認処理に必須の
            # フィールド（event_id/project_page_id等）を守るため表示用テキストを諦める。
            payload["attendee_display"] = "(表示省略、参加者多数)"
            value = json.dumps(payload, ensure_ascii=False)
        return value

    @staticmethod
    def from_button_value(value: str) -> "MeetingCandidate":
        return MeetingCandidate(**json.loads(value))


def _slack_headers() -> dict[str, str]:
    token = os.environ.get("SLACK_BOT_TOKEN")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


def _slack_get(method: str, params: dict[str, str]) -> dict[str, Any]:
    response = requests.get(
        f"{_SLACK_API_BASE}/{method}",
        headers=_slack_headers(),
        params=params,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    return response.json()


def _resolve_dm_channel(rep_email: str) -> tuple[str, str] | None:
    """`rep_email`のSlackユーザーIDを解決し、(DMチャンネルID, SlackユーザーID)を返す
    （失敗時はNone）。
    """
    lookup = _slack_get("users.lookupByEmail", {"email": rep_email})
    if not lookup.get("ok"):
        logger.warning(
            "Slack users.lookupByEmail failed for rep_email=%s: %s",
            rep_email,
            lookup.get("error"),
        )
        return None
    user_id = lookup["user"]["id"]

    open_result = requests.post(
        f"{_SLACK_API_BASE}/conversations.open",
        headers=_slack_headers(),
        json={"users": user_id},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    ).json()
    if not open_result.get("ok"):
        logger.warning(
            "Slack conversations.open failed for rep_email=%s: %s",
            rep_email,
            open_result.get("error"),
        )
        return None
    return open_result["channel"]["id"], user_id


def _alert_delivery_failure(candidate: MeetingCandidate, reason: str) -> None:
    """obasan-qualityレビューBLOCKER対応（2026-08-13）: Slack DM送信が失敗すると、
    ログ（誰も見ていない）以外に気づく手段が無く、案件が永遠にNotionへ登録されない
    まま静かに失われる。既存の運用アラート用Incoming Webhook（`SLACK_WEBHOOK_URL_ALERT`、
    `src/sync_engine/slack_notifier.py`が使うものと同じ環境変数）へフォールバック通知する。
    こちらも未設定なら何もしない（この場合はログのみに留まるが、これ以上できることはない）。
    """
    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
        return
    text = (
        f"[商談アイテム自動検知] 担当営業へのSlack DM送信に失敗しました（{reason}）\n"
        f"対象: {candidate.title}\n"
        f"担当営業: {candidate.rep_email}\n"
        f"案件: {candidate.project_name}\n"
        f"手動でNotionアクション履歴DBへの登録要否をご確認ください。"
    )
    try:
        requests.post(url, json={"text": text}, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("failed to post fallback alert to SLACK_WEBHOOK_URL_ALERT")


def post_approval_request(candidate: MeetingCandidate) -> bool:
    """`candidate`の承認依頼を、担当営業(`candidate.rep_email`)へSlack DMで送る。

    実際にSlackへメッセージが投稿できたかどうかを返す（obasan-qualityレビューBLOCKER
    対応、2026-08-13。以前は成否を一切呼び出し元へ返さず、失敗が誰にも気づかれない
    まま案件が永遠にNotionへ登録されないリスクがあった）。

    `SLACK_BOT_TOKEN`未設定時は連携自体が無効化された状態として何もせずFalseを返す
    （アラートも送らない。意図的な未設定と実際の障害を区別するため）。それ以外の失敗
    （ユーザー解決失敗・chat.postMessage失敗）は`_alert_delivery_failure()`で
    `SLACK_WEBHOOK_URL_ALERT`へフォールバック通知した上でFalseを返す。
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False

    resolved = _resolve_dm_channel(candidate.rep_email)
    if resolved is None:
        _alert_delivery_failure(candidate, "Slackユーザー解決に失敗")
        return False
    channel, user_id = resolved
    candidate = replace(candidate, rep_slack_user_id=user_id)

    summary_text = (
        f"*Googleカレンダー予定から商談アイテムを検知しました*\n"
        f"案件: {candidate.project_name}\n"
        f"日時: {candidate.action_date}\n"
        f"アクション種別: {candidate.action_type}\n"
        f"先方: {candidate.attendee_display}\n"
        f"予定タイトル: {candidate.title}\n"
        f"\n"
        f"_「対象外」にした場合も、この予定が次回以降のカレンダー同期で再度検知されると"
        f"改めて承認依頼が届くことがあります。_"
    )
    button_value = candidate.to_button_value()
    body = {
        "channel": channel,
        "text": f"商談アイテムの自動検知: {candidate.title}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": summary_text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "承認して登録"},
                        "style": "primary",
                        "action_id": APPROVE_ACTION_ID,
                        "value": button_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "対象外"},
                        "action_id": REJECT_ACTION_ID,
                        "value": button_value,
                    },
                ],
            },
        ],
    }
    try:
        response = requests.post(
            f"{_SLACK_API_BASE}/chat.postMessage",
            headers=_slack_headers(),
            json=body,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        result = response.json()
        if not result.get("ok"):
            # Slack Web APIはHTTP 200でもエラーをbody({"ok": false, "error": ...})で
            # 返すため、response.ok（HTTPステータス）だけでは失敗を検知できない。
            logger.warning("Slack chat.postMessage failed: %s", result.get("error"))
            _alert_delivery_failure(candidate, f"chat.postMessage失敗: {result.get('error')}")
            return False
    except Exception:
        logger.exception("unexpected error while posting Slack approval request")
        _alert_delivery_failure(candidate, "chat.postMessage呼び出し中に例外発生")
        return False

    return True


def _update_original_message(response_url: str, text: str) -> None:
    try:
        requests.post(
            response_url,
            json={"replace_original": True, "text": text, "blocks": []},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("unexpected error while updating Slack message via response_url")


def _build_action_properties(candidate: MeetingCandidate) -> dict[str, Any]:
    return {
        _TITLE_PROPERTY: candidate.title,
        _ACTION_TYPE_PROPERTY: candidate.action_type,
        _ACTION_DATE_PROPERTY: candidate.action_date,
        _MEMO_PROPERTY: f"Googleカレンダー予定「{candidate.title}」から自動作成・Slack承認済み",
        _PROJECT_PROPERTY: [candidate.project_page_id],
        _CONTACT_PERSON_PROPERTY: candidate.attendee_display,
        _EVENT_ID_PROPERTY: candidate.event_id,
    }


def handle_interaction(payload: Mapping[str, Any], action_client: ActionNotionClient) -> None:
    """Slack interactivityのpayload（ボタン押下）を処理する。

    承認ボタン: `Googleカレンダーイベントid`で重複確認の上、無ければアクション履歴DBへ
    新規ページを作成する（Rule 4）。却下ボタン: 何もしない。いずれの場合も
    `response_url`で元メッセージを更新し、ボタンを消す。

    shirokuma-secレビューWARN対応（2026-08-13、多層防御）: DMの機密性のみに依存せず、
    実際にボタンを押したSlackユーザー（`payload["user"]["id"]`）が、DM送信先として
    解決した本人（`candidate.rep_slack_user_id`）と一致するかを確認する。
    """
    actions = payload.get("actions") or []
    if not actions:
        return
    action = actions[0]
    action_id = action.get("action_id")
    response_url = payload.get("response_url")
    candidate = MeetingCandidate.from_button_value(action["value"])

    clicking_user_id = (payload.get("user") or {}).get("id")
    if clicking_user_id != candidate.rep_slack_user_id:
        logger.warning(
            "Slack interaction user mismatch: expected=%s actual=%s event_id=%s",
            candidate.rep_slack_user_id,
            clicking_user_id,
            candidate.event_id,
        )
        if response_url:
            _update_original_message(response_url, "この操作は担当営業本人のみ行えます")
        return

    if action_id == REJECT_ACTION_ID:
        if response_url:
            _update_original_message(response_url, f"対象外にしました: {candidate.title}")
        return

    if action_id != APPROVE_ACTION_ID:
        return

    existing_page_id = find_page_id_by_text_property(
        action_client, _EVENT_ID_PROPERTY, candidate.event_id
    )
    if existing_page_id is not None:
        if response_url:
            _update_original_message(response_url, f"既に登録済みです: {candidate.title}")
        return

    action_client.create_page(_build_action_properties(candidate))
    if response_url:
        _update_original_message(response_url, f"✅ 登録しました: {candidate.title}")
