"""メール照合の判断。DB・外部APIに依存しない。"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from uuid import UUID
from email.utils import getaddresses


class Conflict(ValueError):
    pass


class Forbidden(ValueError):
    pass


def same_value(key: str, actual, expected) -> bool:
    if key == "アクション日" and isinstance(actual, str) and isinstance(expected, str):
        try:
            return datetime.fromisoformat(actual.replace("Z", "+00:00")) == datetime.fromisoformat(expected.replace("Z", "+00:00"))
        except ValueError:
            return False
    if isinstance(actual, list) and isinstance(expected, list):
        def canonical(values):
            try:
                return sorted(str(UUID(value)) for value in values)
            except (ValueError, TypeError, AttributeError):
                return sorted(values)
        return canonical(actual) == canonical(expected)
    return actual == expected


def normalized(value: str) -> str:
    return re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", value)).casefold()


def authentication_notice(message: dict) -> bool:
    """返信不可のコード通知と、確認済み送信元の認証通知だけ保存対象から外す。"""
    senders = getaddresses([message.get("from_header") or ""])
    automatic = any(re.fullmatch(r"(?:no[-_.]?reply|do[-_.]?not[-_.]?reply)", address.lower().split("@", 1)[0])
                    for _, address in senders)
    subject = unicodedata.normalize("NFKC", message.get("subject") or "").strip()
    if automatic and re.search(r"認証コード|確認コード|ワンタイム(?:パスワード|パスコード|コード)|verification code|security code|one[- ]time (?:password|passcode|code)", subject, re.I):
        return True
    # info/supportは業務メールにも使われるため、実際に確認した件名との組だけ除外する。
    known_notices = {
        "info@willer.co.jp": "認証コードを送付します",
        "info@kosokubus.com": "【高速バスドットコム】パスワード通知メール",
        "support@up-sider.com": "UPSIDER |【重要】本人認証手続き:取引を承認または拒否してください",
    }
    return len(senders) == 1 and known_notices.get(senders[0][1].lower()) == subject


def participants(message: dict, owner: str, internal_domains: set[str]) -> tuple[str, list[dict]]:
    sender = getaddresses([message.get("from_header", "")])
    outbound = any(address.lower() == owner.lower() for _, address in sender)
    headers = [message.get("to_header", ""), message.get("cc_header", "")] if outbound else [message.get("from_header", "")]
    people = {}
    for name, address in getaddresses(headers):
        address = address.strip().lower()
        if "@" not in address or address == owner.lower() or address.rsplit("@", 1)[1] in internal_domains:
            continue
        people[address] = {"email": address, "name": name.strip()}
    return ("outbound" if outbound else "inbound"), list(people.values())


def unquoted(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if (line.startswith(">") or re.match(r"(?i)^(on .+wrote:|from:|差出人:|[-ー]{2,}\s*(original|forwarded|転送))", line)
                or re.match(r"^.{0,100}(さん|様).*書きました[:：]", line)):
            break
        if line:
            lines.append(line)
    return lines


def evidence(message: dict, direction: str, counterpart: str) -> list[dict]:
    """送信の宛名と受信の相手署名だけを採る。引用・自社署名は採らない。"""
    lines = unquoted(message.get("body_text") or "")
    if direction == "outbound":
        # 宛名の終端（様・御中・各位）まで。本文に現れる顧客名は数えない。
        end = next((i for i, line in enumerate(lines[:8]) if re.search(r"(?:様|御中|各位)[、。\s]*$", line)), None)
        selected = lines[:end + 1] if end is not None else []
        source = "salutation"
    else:
        # 本文後半のメールアドレス、または署名区切りがある範囲に限定する。
        markers = [i for i, line in enumerate(lines) if re.fullmatch(r"[-_=━─＊*]{3,}", line)]
        anchors = [i for i, line in enumerate(lines) if counterpart.casefold() in line.casefold()]
        start = markers[-1] + 1 if markers and markers[-1] < len(lines) - 1 else max(0, anchors[-1] - 4) if anchors else len(lines)
        selected = lines[start:][-15:]
        source = "signature"
    result = []
    for line in selected:
        value = re.sub(r"(?:御中|各位)[、。\s]*$", "", line).strip()
        if value:
            result.append({"source": source, "value": value[:300], "message_id": message["id"]})
    return result


def match(evidence_rows: list[dict], masters: list[dict], affiliations: list[str]) -> dict:
    candidates = []
    for master in masters:
        name = normalized(master["name"])
        hits = [item for item in evidence_rows if normalized(item["value"]) == name]
        if hits:
            # 同じ会社名でも宛名と署名は2根拠。同じ出典の繰り返しは増やさない。
            candidates.append({**master, "evidence": hits, "sources": sorted({h["source"] for h in hits})})
    qualified = [item for item in candidates if len(item["sources"]) >= 2]
    if len(qualified) != 1:
        return {"state": "pending", "reason": "候補が複数あります" if len(qualified) > 1 else "一致する根拠を確認してください", "candidates": candidates}
    winner = qualified[0]
    if affiliations and set(affiliations) != {winner["id"]}:
        return {"state": "pending", "reason": "既存の所属と一致しません", "candidates": candidates}
    return {"state": "approved", "reason": "宛名と相手の署名が一致", "company_id": winner["id"], "candidates": candidates}


def identity(message: dict, owner: str) -> str:
    """複数担当者が同じメールを取得しても営業記録は1件。"""
    rfc_id = (message.get("message_id_header") or "").strip()
    senders = [address.lower() for _, address in getaddresses([message.get("from_header") or ""]) if address]
    value = ("rfc:" + rfc_id.strip("<>") + ":" + ",".join(sorted(senders))) if rfc_id else "gmail:" + owner.lower() + ":" + message["id"]
    return hashlib.sha256(value.encode()).hexdigest()


def decide(row: dict, *, revision: int, action: str, selection: dict, actor: str, channel: str) -> dict:
    if revision != row["revision"]:
        raise Conflict("別の画面で更新されています。読み直してください")
    if row["state"] in {"processing", "evaluating", "reconciling", "recheck"} or row["state"] == "unknown" and action != "reconcile":
        raise Conflict("登録の結果を照合中です。追加登録は行いません")
    if action not in {"confirm", "hold", "exclude", "resume", "reconcile"}:
        raise ValueError("操作が不正です")
    if action == "reconcile" and row["state"] != "unknown":
        raise Conflict("登録結果が不明な記録だけを照合できます")
    if row.get("action_id") and action in {"exclude", "hold"}:
        raise Conflict("記録作成後は関連先を訂正してください。履歴は削除しません")
    next_row = dict(row)
    if action == "confirm":
        if not selection.get("company_id") or not selection.get("contact_id"):
            raise ValueError("所属会社と連絡先を選んでください")
        next_row.update(selection)
        next_row["state"] = "approved"
        next_row["delivery"] = "queued"
    else:
        next_row["state"] = {"hold": "held", "exclude": "excluded", "resume": "new", "reconcile": "recheck"}[action]
    next_row.update(revision=revision + 1, actor=actor, channel=channel)
    return next_row
