"""Slack App Home・入力画面・選択肢の受信口。"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Mapping
from urllib.parse import parse_qs

from src.slack_crm import storage, views
from src.slack_crm.domain import changed_fields, validate_changes
from src.slack_crm.slack_api import call
from src.db_schema.action import ACTION_SCHEMA
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers.slack_interaction_webhook import _verify_slack_signature
from src.sync_engine.webhook_handlers._common import unauthorized_response
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value

logger = logging.getLogger(__name__)

def _response(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"statusCode": 200, "body": json.dumps(body or {}, ensure_ascii=False)}


def _authorized(payload: Mapping[str, Any]) -> bool:
    team = payload.get("team") or {}
    team_id = team.get("id") if isinstance(team, dict) else payload.get("team_id")
    user = payload.get("user") or {}
    user_id = user.get("id") if isinstance(user, dict) else user
    allowed = {part.strip() for part in os.environ.get("SLACK_CRM_ALLOWED_USER_IDS", "").split(",") if part.strip()}
    return bool(allowed and team_id == os.environ.get("SLACK_CRM_ALLOWED_TEAM_ID") and user_id in allowed)


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = ((payload.get("view") or {}).get("private_metadata") or "{}")
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _state(payload: Mapping[str, Any], block_id: str, action_id: str) -> Any:
    return (((payload.get("view") or {}).get("state") or {}).get("values") or {}).get(block_id, {}).get(action_id) or {}


def _page_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("対象IDが不正です") from exc


def _action_client() -> HttpNotionClient:
    return HttpNotionClient(ACTION_SCHEMA.key, ACTION_SCHEMA.notion_database_id,
                            timeout=1.5, max_retries=0, max_rate_limit_retries=0)


def _action_options(project_id: str, query: str) -> list[dict[str, Any]]:
    pages = _action_client().query_page(
        page_size=20,
        filter={"property": "案件名", "relation": {"contains": _page_id(project_id)}},
        sorts=[{"property": "アクション日", "direction": "descending"}],
    )
    result = []
    for page in pages:
        props = page.get("properties") or {}
        raw = props.get("商談回数・電話回数・メール回数（何回目）")
        title = parse_notion_property_value(raw) if raw else "名称なし"
        day_raw = props.get("アクション日")
        day = parse_notion_property_value(day_raw) if day_raw else ""
        memo_raw = props.get("履歴メモ")
        memo = parse_notion_property_value(memo_raw) if memo_raw else ""
        label = f"{day or '日付なし'} {title or '名称なし'}｜{str(memo or 'メモなし')[:24]}｜{str(page['id'])[-8:]}"
        if query and query.lower() not in label.lower():
            continue
        result.append({"text": views.plain(label[:75]), "value": _page_id(page["id"])})
    return result[:20]


def _user_id(payload: Mapping[str, Any]) -> str:
    user = payload.get("user") or {}
    return str(user.get("id") if isinstance(user, dict) else user or "")


def _on_suggestion(payload: dict[str, Any]) -> dict[str, Any]:
    action_id = payload.get("action_id")
    query = str(payload.get("value") or "")
    if action_id == "crm_project_options":
        projects = storage.search_projects(query)
        return _response({"options": [
            {"text": views.plain(f"{str(p.get('案件名') or '名称なし')[:48]}｜{str(p.get('client_name') or '取引先未設定')[:38]}｜{str(p.get('営業ステータス') or '')[:12]}"),
             "value": p["id"]}
            for p in projects
        ]})
    if action_id == "crm_action_options":
        project_id = _metadata(payload).get("project_id")
        return _response({"options": _action_options(project_id, query) if project_id else []})
    return _response({"options": []})


def _form_values(payload: Mapping[str, Any], kind: str, operation_id: str) -> dict[str, Any]:
    if kind == "project_update":
        values = {}
        status = _state(payload, "status", "status_value").get("selected_option")
        confidence = _state(payload, "confidence", "confidence_value").get("selected_option")
        date_state = _state(payload, "date", "next_date")
        day = date_state.get("selected_date")
        if status:
            values["営業ステータス"] = status["value"]
        if confidence:
            values["確度"] = confidence["value"]
        if date_state:
            values["次回アクション日"] = day
        return values
    values = {}
    kind_option = _state(payload, "action_type", "type_value").get("selected_option")
    date_state = _state(payload, "date", "action_date")
    day = date_state.get("selected_date")
    memo_state = _state(payload, "memo", "memo_value")
    memo = memo_state.get("value")
    if kind_option:
        values["アクション種別"] = kind_option["value"]
    if date_state:
        values["アクション日"] = day
    if memo is not None or (kind == "action_update" and memo_state):
        values["履歴メモ"] = memo or ""
    if kind == "action_create" and kind_option and day:
        summary = str(memo or "記録").strip()[:24]
        values["商談回数・電話回数・メール回数（何回目）"] = (
            f"【{kind_option['value']}】{day} {summary} #{operation_id[:10]}")
    return values


def _on_submission(payload: dict[str, Any]) -> dict[str, Any]:
    view = payload.get("view") or {}
    callback = view.get("callback_id")
    meta = _metadata(payload)
    if callback == "crm_pick_project":
        choice = _state(payload, "project_choice", "crm_project_options").get("selected_option")
        project = storage.get_project(_page_id(choice["value"])) if choice else None
        if not project:
            return _response({"response_action": "errors", "errors": {"project_choice": "案件を選び直してください"}})
        target = (views.action_form(project["id"], project_name=project.get("案件名", ""))
                  if meta.get("mode") == "action_create" else views.project_detail(project))
        return _response({"response_action": "update", "view": target})
    if callback == "crm_pick_action":
        choice = _state(payload, "action_choice", "crm_action_options").get("selected_option")
        project_id = _page_id(meta.get("project_id"))
        if not choice:
            return _response({"response_action": "errors", "errors": {"action_choice": "アクションを選んでください"}})
        action_id = _page_id(choice["value"])
        action = _action_client().get_page(action_id)
        if not action or project_id not in (action.get("案件名") or []):
            return _response({"response_action": "errors", "errors": {"action_choice": "対象を確認できません"}})
        return _response({"response_action": "update", "view": views.action_form(
            project_id, project_name=meta.get("project_name", ""), action_id=action_id, current=action)})
    if callback == "crm_preview":
        return _on_save(payload, meta)
    kinds = {"crm_submit_project": "project_update", "crm_submit_action_create": "action_create",
             "crm_submit_action_edit": "action_update"}
    if callback not in kinds:
        return _response()
    kind = kinds[callback]
    try:
        target_id = _page_id(meta.get("project_id") if kind != "action_update" else meta.get("action_id"))
        operation_id = str(meta.get("operation_id") or uuid.uuid4().hex)
        proposed = _form_values(payload, kind, operation_id)
        expected = meta.get("expected") or {}
        for date_key in ("次回アクション日", "アクション日"):
            original = expected.get(date_key)
            if isinstance(original, str) and "T" in original and proposed.get(date_key) == original[:10]:
                proposed.pop(date_key, None)
        changes = proposed if kind == "action_create" else changed_fields(expected, proposed)
        if not changes:
            return _response({"response_action": "update", "view": views.receipt_view("変更はありません。")})
        changes = validate_changes(kind, changes)
    except ValueError as exc:
        message = str(exc)
        block = ("action_type" if "種別" in message else "memo" if "メモ" in message else
                 "status" if "ステータス" in message else "confidence" if "確度" in message else "date")
        return _response({"response_action": "errors", "errors": {block: message}})
    expected = ({key: expected.get(key) for key in changes}
                if kind != "action_create" else {})
    if kind == "action_update":
        expected["案件名"] = [_page_id(meta.get("project_id"))]
    return _response({"response_action": "push", "view": views.preview_view(
        actor_id=_user_id(payload), kind=kind, target_id=target_id,
        expected=expected, changes=changes, operation_id=operation_id)})


def _on_save(payload: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    if meta.get("actor_id") != _user_id(payload):
        return _response({"response_action": "update", "view": views.receipt_view("本人の画面で開き直してください。")})
    try:
        issued_at = int(meta.get("issued_at"))
    except (TypeError, ValueError):
        issued_at = 0
    if not 0 <= time.time() - issued_at <= 15 * 60:
        return _response({"response_action": "update", "view": views.receipt_view(
            "画面の有効時間を過ぎました。案件を開き直してください。")})
    if os.environ.get("SLACK_CRM_WRITE_ENABLED", "").lower() != "true":
        return _response({"response_action": "update", "view": views.receipt_view("更新機能の準備中です。")})
    try:
        kind = meta.get("kind")
        target_id = _page_id(meta.get("target_id"))
        changes = validate_changes(kind, meta.get("changes") or {})
        expected = meta.get("expected") or {}
        inserted = storage.enqueue(operation_id=meta["operation_id"], actor_id=_user_id(payload),
                                   kind=kind, target_id=target_id, expected=expected, changes=changes)
    except (ValueError, KeyError):
        return _response({"response_action": "update", "view": views.receipt_view("保存内容を確認できません。開き直してください。")})
    result = _response({"response_action": "update", "view": views.receipt_view(
        f"更新を受け付けました（受付番号: {meta['operation_id'][:8]}）。結果を本人DMへ送ります。")})
    if inserted:
        result["_background_operation_id"] = meta["operation_id"]
    return result


def _on_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    meta = _metadata(payload)
    trigger = payload.get("trigger_id")
    if action_id in {"crm_home_find", "crm_home_action"}:
        call("views.open", {"trigger_id": trigger, "view": views.project_picker(
            mode="action_create" if action_id == "crm_home_action" else "detail")})
        return _response()
    if action_id == "crm_home_open_project":
        project = storage.get_project(_page_id(action.get("value")))
        call("views.open", {"trigger_id": trigger, "view": views.project_detail(project) if project
             else views.receipt_view("案件を確認できません。検索から開き直してください。")})
        return _response()
    project_id = meta.get("project_id")
    if action_id in {"crm_open_project_edit", "crm_open_action_create", "crm_open_action_pick"}:
        project = storage.get_project(_page_id(project_id)) if project_id else None
        if not project:
            call("views.push", {"trigger_id": trigger, "view": views.receipt_view(
                "案件を確認できません。検索から開き直してください。")})
            return _response()
        if action_id == "crm_open_project_edit":
            target = views.project_form(project)
        elif action_id == "crm_open_action_create":
            target = views.action_form(project["id"], project_name=project.get("案件名", ""))
        else:
            target = views.action_picker(project["id"], project.get("案件名", ""))
        call("views.push", {"trigger_id": trigger, "view": target})
        return _response()
    return _response()


def publish_home(user_id: str) -> None:
    recent = storage.recent_projects(user_id)
    call("views.publish", {"user_id": user_id, "view": views.home_view(recent)})


def handler(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(event.get("body") or "")
    if not _verify_slack_signature(event.get("headers") or {}, raw):
        return unauthorized_response()
    content_type = next((str(value) for key, value in (event.get("headers") or {}).items()
                         if key.lower() == "content-type"), "")
    try:
        if "application/json" in content_type:
            envelope = json.loads(raw)
            event_payload = envelope.get("event") or {}
            payload = {"type": "event_callback", "team": {"id": envelope.get("team_id")},
                       "user": {"id": event_payload.get("user")}}
            if envelope.get("type") == "url_verification":
                expected_team = os.environ.get("SLACK_CRM_ALLOWED_TEAM_ID")
                if expected_team and envelope.get("team_id") != expected_team:
                    return _response()
                return _response({"challenge": envelope.get("challenge")})
            if not _authorized(payload):
                return _response()
            if event_payload.get("type") == "app_home_opened":
                return {**_response(), "_background_home_user": event_payload["user"]}
            return _response()
        values = parse_qs(raw)
        payload = json.loads((values.get("payload") or ["{}"]) [0])
    except (ValueError, TypeError, KeyError):
        return {"statusCode": 400, "body": "invalid Slack request"}
    if not _authorized(payload):
        return _response()
    kind = payload.get("type")
    try:
        if kind == "block_suggestion":
            return _on_suggestion(payload)
        if kind == "view_submission":
            return _on_submission(payload)
        if kind == "block_actions":
            return _on_action(payload)
    except Exception:
        logger.exception("Slack CRM画面の処理に失敗")
        if kind == "block_suggestion":
            return _response({"options": []})
        if kind == "view_submission":
            return _response({"response_action": "update", "view": views.receipt_view(
                "読み込めませんでした。画面を開き直してください。")})
        return {"statusCode": 500, "body": "Slack CRM request failed"}
    return _response()
