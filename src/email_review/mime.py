"""本文だけを有界に取り出す。添付・転送メール・HTMLは自動判定に使わない。"""
import base64
import re
from src.email_review.body import current_body

LIMIT = 20000


def plain_text(payload: dict) -> str:
    return _plain_text(payload) or ""


def _plain_text(payload: dict) -> str | None:
    if payload.get("filename") or payload.get("mimeType") == "message/rfc822":
        return ""
    if payload.get("mimeType") == "text/plain":
        encoded = (payload.get("body") or {}).get("data") or ""
        if not encoded or len(encoded) > 200000:
            return None
        content_type = next((h.get("value", "") for h in payload.get("headers", []) if h.get("name", "").lower() == "content-type"), "")
        match = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I)
        charset = match.group(1).lower() if match else "utf-8"
        if charset not in {"utf-8", "us-ascii", "iso-2022-jp", "shift_jis", "shift-jis", "windows-31j", "cp932"}:
            return None
        charset = {"shift-jis": "shift_jis", "windows-31j": "cp932"}.get(charset, charset)
        try:
            text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(charset)
        except (ValueError, UnicodeError):
            return None
        text = current_body(text)
        return text if len(text) <= LIMIT else None
    if str(payload.get("mimeType", "")).startswith("multipart/"):
        children = payload.get("parts") or []
        if len(children) > 30:
            return None
        parts = [_plain_text(part) for part in children]
        if any(part is None for part in parts):
            return None
        text = "\n".join(part for part in parts if part)
        return text if len(text) <= LIMIT else None
    return ""
