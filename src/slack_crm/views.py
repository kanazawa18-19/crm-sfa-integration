"""携帯で短く読めるSlack Block Kit画面。"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.db_schema.project import ACTIVE_STATUSES, CONFIDENCE_LEVELS, PROJECT_SCHEMA
from src.db_schema.action import ACTION_SCHEMA


def plain(text: str) -> dict[str, str]:
    return {"type": "plain_text", "text": text[:150], "emoji": True}


def _button(label: str, action_id: str, value: str = "") -> dict[str, Any]:
    return {"type": "button", "text": plain(label), "action_id": action_id, "value": value}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def _modal(title: str, callback_id: str, blocks: list[dict[str, Any]], *,
           metadata: dict[str, Any] | None = None, submit: str | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {"type": "modal", "title": plain(title),
                            "callback_id": callback_id, "blocks": blocks,
                            "private_metadata": json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))}
    if submit:
        view["submit"] = plain(submit)
    return view


def home_view(recent: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        _section("*CRM-SFA*\n携帯から案件とアクションを更新できます。"),
        {"type": "actions", "elements": [
            _button("案件を探す", "crm_home_find"),
            _button("アクションを記録", "crm_home_action"),
        ]},
    ]
    if recent:
        blocks.append(_section("*最近更新した案件*"))
        for project in recent[:3]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": f"*{_display(project.get('案件名'))}*｜{_display(project.get('client_name'))}｜{_display(project.get('営業ステータス'))}"},
                           "accessory": _button("開く", "crm_home_open_project", project["id"])})
    blocks.append(_section("変更前後を確認してから保存します。結果は本人のDMへ届きます。"))
    return {"type": "home", "blocks": blocks}


def project_picker(*, mode: str) -> dict[str, Any]:
    return _modal("案件を選ぶ", "crm_pick_project", [{
        "type": "input", "block_id": "project_choice", "label": plain("案件名・取引先名"),
        "element": {"type": "external_select", "action_id": "crm_project_options",
                    "min_query_length": 2, "placeholder": plain("2文字以上入力")},
    }], metadata={"mode": mode}, submit="次へ")


def _display(value: Any) -> str:
    if value is None or value == "":
        return "未設定"
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:160]


def project_detail(project: dict[str, Any]) -> dict[str, Any]:
    snapshot = {key: project.get(key) for key in ("営業ステータス", "次回アクション日", "確度")}
    return _modal("案件", "crm_project_detail", [
        _section(f"*{_display(project.get('案件名'))}*\n取引先: {_display(project.get('client_name'))}\n営業ステータス: {_display(snapshot['営業ステータス'])}\n次回アクション日: {_display(snapshot['次回アクション日'])}\n確度: {_display(snapshot['確度'])}"),
        {"type": "actions", "elements": [
            _button("案件を更新", "crm_open_project_edit"),
            _button("アクションを記録", "crm_open_action_create"),
            _button("アクションを直す", "crm_open_action_pick"),
        ]},
    ], metadata={"project_id": project["id"], "project_name": project.get("案件名", ""), "snapshot": snapshot})


def _select(label: str, block_id: str, action_id: str, values: list[str], current: str | None,
            *, optional: bool = False) -> dict[str, Any]:
    options = [{"text": plain(value), "value": value} for value in values]
    element: dict[str, Any] = {"type": "static_select", "action_id": action_id,
                               "options": options, "placeholder": plain(label)}
    if current in values:
        element["initial_option"] = {"text": plain(current), "value": current}
    return {"type": "input", "block_id": block_id, "label": plain(label),
            "optional": optional, "element": element}


def project_form(project: dict[str, Any]) -> dict[str, Any]:
    snapshot = {key: project.get(key) for key in ("営業ステータス", "次回アクション日", "確度")}
    status_values = [p for p in next(p.options for p in PROJECT_SCHEMA.properties if p.name == "営業ステータス")
                     if p in ACTIVE_STATUSES]
    blocks: list[dict[str, Any]] = [_section(f"*{_display(project.get('案件名'))}* の変更")]
    if snapshot["営業ステータス"] in ACTIVE_STATUSES:
        blocks.append(_select("営業ステータス", "status", "status_value", status_values, snapshot["営業ステータス"]))
    else:
        blocks.append(_section("契約・失注・解約後の状態変更はCRM画面で行ってください。"))
    date_element: dict[str, Any] = {"type": "datepicker", "action_id": "next_date"}
    if snapshot["次回アクション日"]:
        date_element["initial_date"] = str(snapshot["次回アクション日"])[:10]
    blocks.append({"type": "input", "block_id": "date", "label": plain("次回アクション日"),
                   "optional": True, "element": date_element})
    blocks.append(_select("確度", "confidence", "confidence_value", list(CONFIDENCE_LEVELS), snapshot["確度"], optional=True))
    return _modal("案件を更新", "crm_submit_project", blocks,
                  metadata={"project_id": project["id"], "project_name": project.get("案件名", ""),
                            "expected": snapshot, "operation_id": uuid.uuid4().hex},
                  submit="確認へ")


def action_picker(project_id: str, project_name: str = "") -> dict[str, Any]:
    return _modal("アクションを選ぶ", "crm_pick_action", [{
        "type": "input", "block_id": "action_choice", "label": plain("最近のアクション"),
        "element": {"type": "external_select", "action_id": "crm_action_options",
                    "min_query_length": 0, "placeholder": plain("アクションを選択")},
    }], metadata={"project_id": project_id, "project_name": project_name}, submit="次へ")


def action_form(project_id: str, *, project_name: str = "", action_id: str | None = None,
                current: dict[str, Any] | None = None) -> dict[str, Any]:
    current = current or {}
    creating = action_id is None
    blocks: list[dict[str, Any]] = [_section(f"*{_display(project_name)}* のアクション")]
    types = list(next(p.options for p in ACTION_SCHEMA.properties if p.name == "アクション種別"))
    blocks.append(_select("種別", "action_type", "type_value", types, current.get("アクション種別")))
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    date_element: dict[str, Any] = {"type": "datepicker", "action_id": "action_date",
                                    "initial_date": str(current.get("アクション日") or today)[:10]}
    blocks.append({"type": "input", "block_id": "date", "label": plain("アクション日"), "optional": not creating,
                   "element": date_element})
    memo = str(current.get("履歴メモ") or "")
    if len(memo) > 500:
        blocks.append(_section("このアクションのメモは長いため、変更はCRM画面で行ってください。"))
    else:
        memo_element: dict[str, Any] = {"type": "plain_text_input", "action_id": "memo_value",
                                        "multiline": True, "max_length": 500,
                                        "placeholder": plain("要点だけを記録")}
        if memo:
            memo_element["initial_value"] = memo
        blocks.append({"type": "input", "block_id": "memo", "label": plain("メモ"),
                       "optional": True, "element": memo_element})
    expected = {key: current.get(key) for key in ("アクション種別", "アクション日")}
    if len(memo) <= 500:
        expected["履歴メモ"] = current.get("履歴メモ")
    if not creating:
        expected["案件名"] = [project_id]
    return _modal("アクションを記録" if creating else "アクションを更新",
                  "crm_submit_action_create" if creating else "crm_submit_action_edit", blocks,
                  metadata={"project_id": project_id, "action_id": action_id, "project_name": project_name,
                            "expected": expected, "operation_id": uuid.uuid4().hex}, submit="確認へ")


def preview_view(*, actor_id: str, kind: str, target_id: str, expected: dict[str, Any],
                 changes: dict[str, Any], operation_id: str) -> dict[str, Any]:
    lines = [f"• *{_display(key)}*: {_display(expected.get(key))} → {_display(value)}"
             for key, value in changes.items()]
    return _modal("保存内容を確認", "crm_preview", [
        _section("\n".join(lines)),
    ], metadata={"actor_id": actor_id, "kind": kind, "target_id": target_id,
                "expected": expected, "changes": changes, "operation_id": operation_id,
                "issued_at": int(time.time())}, submit="保存する") | {"close": plain("戻る")}


def receipt_view(message: str) -> dict[str, Any]:
    return _modal("CRM-SFA", "crm_receipt", [_section(message)]) | {"clear_on_close": True}
