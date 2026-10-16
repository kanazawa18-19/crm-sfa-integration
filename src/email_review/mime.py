"""本文だけを有界に取り出す。添付・転送メール・HTMLは自動判定に使わない。"""
import base64
import re

LIMIT = 20000


def plain_text(payload: dict) -> str:
    if payload.get("filename") or payload.get("mimeType") == "message/rfc822":
        return ""
    if payload.get("mimeType") == "text/plain":
        encoded = (payload.get("body") or {}).get("data") or ""
        if len(encoded) > 200000:
            return ""
        content_type = next((h.get("value", "") for h in payload.get("headers", []) if h.get("name", "").lower() == "content-type"), "")
        match = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I)
        charset = match.group(1).lower() if match else "utf-8"
        if charset not in {"utf-8", "us-ascii", "iso-2022-jp", "shift_jis", "shift-jis", "windows-31j", "cp932"}:
            return ""
        charset = {"shift-jis": "shift_jis", "windows-31j": "cp932"}.get(charset, charset)
        try:
            text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(charset)
        except (ValueError, UnicodeError):
            return ""
        return text if len(text) <= LIMIT else ""
    if str(payload.get("mimeType", "")).startswith("multipart/"):
        parts = [plain_text(part) for part in (payload.get("parts") or [])[:30]]
        text = "\n".join(part for part in parts if part)
        return text if len(text) <= LIMIT else ""
    return ""
