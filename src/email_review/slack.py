"""Slackの照合画面。管理画面と同じ受付ID・版・本人確認を使う。"""
from __future__ import annotations

import json
import os

from src.email_review.domain import Conflict, Forbidden
from src.email_review.repository import Repository
from src.email_review.service import Service, enabled
from src.slack_crm.slack_api import call
from src.slack_crm.views import plain, _modal, receipt_view
from src.slack_crm.storage import search_projects


def handles(payload: dict) -> bool:
    ids = [payload.get("action_id", ""), (payload.get("view") or {}).get("callback_id", "")]
    ids += [a.get("action_id", "") for a in payload.get("actions") or []]
    return any(str(value).startswith("crm_email_") for value in ids)


def actor(payload: dict, repo: Repository) -> str:
    team = payload.get("team") or {}
    expected_team = os.environ.get("SLACK_CRM_ALLOWED_TEAM_ID")
    if not expected_team or team.get("id") != expected_team:
        raise Forbidden("対象のワークスペースで開いてください")
    user_id = (payload.get("user") or {}).get("id")
    if not user_id:
        raise Forbidden("Slackの本人情報がありません")
    user = (call("users.info", {"user": user_id}).get("user") or {})
    if user.get("id") != user_id or user.get("team_id") != expected_team or user.get("deleted") or user.get("is_bot") or user.get("is_restricted") or user.get("is_ultra_restricted"):
        raise Forbidden("本人のSlackアカウントで開いてください")
    email = (user.get("profile") or {}).get("email")
    if not email:
        raise Forbidden("Slackのメールアドレスを確認できません")
    return repo.slack_actor(email)


def option(name: str, value: str) -> dict:
    return {"text": plain(name[:75]), "value": value}


def selected(payload: dict, key: str) -> str | None:
    state = ((payload.get("view") or {}).get("state") or {}).get("values") or {}
    field = state.get(key, {})
    return (((field.get("crm_email_" + key) or field.get(key) or {}).get("selected_option") or {}).get("value"))


def picker(repo, actor_id: str) -> dict:
    labels = {"pending": "確認待ち", "held": "保留", "approved": "確定済み", "excluded": "対象外"}
    groups = []
    for state, label in labels.items():
        rows = repo.list(actor_id, state=state)[:25]
        if rows:
            groups.append({"label": plain(label), "options": [option((r["participants"][0]["email"][:25] + "｜" + r["subject"][:38] + "｜" + r["id"][-6:]), r["id"]) for r in rows]})
    if not groups:
        return receipt_view("メールの照合対象はありません。")
    blocks = [{"type": "input", "block_id": "review",
        "label": plain("メールを選ぶ（各状態の最新25件）"),
        "element": {"type": "static_select", "action_id": "review", "option_groups": groups}}]
    base = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    if base.startswith("https://"):
        blocks.append({"type": "actions", "elements": [{"type": "button", "text": plain("全件を管理画面で見る"), "url": base + "/email-review", "action_id": "crm_email_dashboard"}]})
    return _modal("メールの照合", "crm_email_pick", blocks, submit="開く")


def detail(row: dict) -> dict:
    labels = {"new": "照合待ち", "evaluating": "照合中", "pending": "確認待ち", "held": "保留", "excluded": "対象外", "approved": "処理待ち", "processing": "記録中", "done": "記録作成済み", "unknown": "結果照合待ち", "not_started": "未配送", "queued": "配送待ち", "delivered": "配送処理完了", "partial": "一部未反映", "recheck": "結果照合待ち", "reconciling": "結果照合中"}
    def section(text):
        return {"type": "section", "text": {"type": "plain_text", "text": text[:2900]}}
    blocks = [section(row["subject"]), section("相手: " + "、".join(p["email"] for p in row["participants"])),
              section(row["reason"]), section(f"記録: {labels.get(row['state'], row['state'])} ／ 配送: {labels.get(row['delivery'], row['delivery'])}")]
    evidence = row.get("evidence") or []
    if evidence:
        blocks.append(section("照合根拠\n" + "\n".join(
            ("宛名" if e["source"] == "salutation" else "相手の署名") + ": " + e["value"] for source in ("salutation", "signature") for e in [item for item in evidence if item["source"] == source][:4])))
    if row.get("direction") != "outbound" or row.get("sent_evidence") is not True:
        blocks.append(section("送信済みと確認できないため、登録・訂正・再照合はできません。下書きは対象外です。送信済みメールの場合は管理者に元メールの状態の再取得を依頼し、その後この画面を開き直してください。"))
        return _modal("メールの照合", "crm_email_decide", blocks,
                      metadata={"id": row["id"], "revision": row["revision"]})
    contacts = row.get("contacts") or []
    if contacts:
        element = {"type": "static_select", "action_id": "contact_id", "options": [option(c["name"], c["id"]) for c in contacts[:100]]}
        current = next((c for c in contacts if c["id"] == row.get("contact_id")), contacts[0] if len(contacts) == 1 else None)
        if current:
            element["initial_option"] = option(current["name"], current["id"])
        blocks.append({"type": "input", "block_id": "contact_id", "optional": True, "label": plain("相手の連絡先"), "element": element})
    else:
        blocks.append({"type": "section", "text": plain("連絡先が未登録です。CRMで登録した後に再照合してください。")})
    for key, label in [("company_id", "相手の所属会社"), ("target_id", "今回の対象施設（空欄なら所属会社）"), ("project_id", "今回の案件（任意）")]:
        element = {"type": "external_select", "action_id": "crm_email_" + key, "min_query_length": 2, "placeholder": plain("2文字以上で検索")}
        value = row.get(key)
        candidate = next((c for c in row.get("candidates", []) if c["id"] == value), None)
        if value:
            element["initial_option"] = option(candidate["name"] if candidate else row.get({"company_id": "company_name", "target_id": "target_name", "project_id": "project_name"}[key]) or "現在の選択 " + value[-8:], value)
        blocks.append({"type": "input", "block_id": key, "optional": True, "label": plain(label), "element": element})
    blocks.append({"type": "input", "block_id": "action", "label": plain("行う操作"), "element": {
        "type": "static_select", "action_id": "action", "options": [option(label, value) for value, label in [
            ("confirm", "関連先を確定・訂正"), ("hold", "保留"), ("exclude", "営業記録の対象外"), ("resume", "再照合"), ("reconcile", "登録済みの記録を探す（再登録なし）")]]}})
    return _modal("メールの照合", "crm_email_decide", blocks, metadata={"id": row["id"], "revision": row["revision"]}, submit="受け付ける")


def handle(payload: dict) -> dict:
    def response(body=None):
        return {"statusCode": 200, "body": json.dumps(body or {}, ensure_ascii=False)}
    if not enabled():
        return response({"response_action": "update", "view": receipt_view("メール照合は準備中です。")})
    repo = Repository()
    kind = payload.get("type")
    try:
        actor_id = actor(payload, repo)
        meta = json.loads((payload.get("view") or {}).get("private_metadata") or "{}")
        if kind == "block_actions":
            if (payload.get("actions") or [{}])[0].get("action_id") == "crm_email_dashboard":
                return response()
            call("views.open", {"trigger_id": payload.get("trigger_id"), "view": picker(repo, actor_id)})
            return response()
        if kind == "block_suggestion":
            repo.get(meta.get("id"), actor_id)
            query = str(payload.get("value") or "")[:80]
            field = str(payload.get("action_id") or "").removeprefix("crm_email_")
            rows = ([{"id": p["id"], "name": p.get("案件名") or "名称なし"} for p in search_projects(query)]
                    if field == "project_id" else repo.masters(query=query))
            return response({"options": [option(r["name"], r["id"]) for r in rows]})
        if (payload.get("view") or {}).get("callback_id") == "crm_email_pick":
            return response({"response_action": "update", "view": detail(repo.get(selected(payload, "review"), actor_id))})
        if (payload.get("view") or {}).get("callback_id") == "crm_email_decide":
            selection = {key: selected(payload, key) for key in ("company_id", "contact_id", "target_id", "project_id")}
            Service(repo=repo).decision(meta["id"], actor_id, meta["revision"], selected(payload, "action"), selection, "slack")
            return response({"response_action": "update", "view": receipt_view("判断を受け付けました。記録・配送の結果はメールの照合から確認できます。")})
    except (Conflict, Forbidden, ValueError, KeyError) as exc:
        if kind == "block_suggestion":
            return response({"options": []})
        if kind == "view_submission":
            return response({"response_action": "errors", "errors": {"review" if (payload.get("view") or {}).get("callback_id") == "crm_email_pick" else "action": str(exc)}})
        return response()
    return response()
