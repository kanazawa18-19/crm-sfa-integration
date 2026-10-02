"""Slack画面の公開と本人への結果通知。"""
from __future__ import annotations

import os
from typing import Any

import requests

_BASE = "https://slack.com/api"


def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise ValueError("SLACK_BOT_TOKEN is not set")
    response = requests.post(
        f"{_BASE}/{method}", json=payload,
        headers={"Authorization": f"Bearer {token}"}, timeout=(0.4, 1.5),
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API {method}: {data.get('error', 'unknown')}")
    return data


def send_result(user_id: str, operation: dict[str, Any], result: dict[str, Any]) -> None:
    state = result["state"]
    if state == "done":
        sync_state = operation.get("syncState")
        if sync_state == "done":
            message = "CRM正本へ保存し、連携先への同期処理も完了しました。"
        elif sync_state == "unmapped":
            message = "CRM正本へ保存しました。連携先の対応レコードがなく、他ツールへの反映は保留です。"
        else:
            message = "CRM正本へ保存しました。連携先に未反映の項目があります。CRMで状態を確認してください。"
    elif state == "conflict":
        message = "CRMの値が別の場所で変わりました。再度開いて確認してください。"
    elif state == "unknown":
        message = "CRMの応答を確認できませんでした。再操作せず、CRMの現在値を確認してください。"
    else:
        message = "CRMを更新できませんでした。現在値を確認してからやり直してください。"
    labels = {"project_update": "案件の更新", "action_update": "アクションの更新", "action_create": "アクションの記録"}
    target_id = result.get("page_id") or operation["targetId"]
    link = "https://www.notion.so/" + str(target_id).replace("-", "")
    changes = operation.get("changes") or {}
    summary = "、".join(f"{key}: {value}" for key, value in changes.items() if key != "履歴メモ")
    if "履歴メモ" in changes:
        summary += "、メモ更新" if summary else "メモ更新"
    message = (f"{labels.get(operation['kind'], 'CRM更新')}｜受付番号 {operation['id'][:8]}\n"
               f"{message}\n対象: <{link}|CRMの対象を開く>\n変更: {summary or 'なし'}")
    dm = call("conversations.open", {"users": user_id})
    channel = (dm.get("channel") or {}).get("id")
    if not channel:
        raise ValueError("Slack DMの宛先を確認できません")
    call("chat.postMessage", {"channel": channel, "text": message})
