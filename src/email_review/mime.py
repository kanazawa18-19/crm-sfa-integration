"""本文だけを有界に取り出す。添付・転送メールは除外し、HTMLは実行せず文字へ変換する。"""
import base64
import re
from src.email_review.body import current_body

LIMIT = 40000


def plain_text(payload: dict) -> str:
    try:
        validate_payload(payload)
        return _plain_text(payload) or ""
    except (ValueError, TypeError, UnicodeError):
        return ""


def _plain_text(payload: dict) -> str | None:
    if payload.get("filename") or payload.get("mimeType") == "message/rfc822":
        return ""
    if payload.get("mimeType") in {"text/plain", "text/html"}:
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
        if payload.get("mimeType") == "text/html":
            from src.email_review.html_body import html_text
            text = html_text(text)
        text = current_body(text)
        return text if within_limit(text) else None
    if str(payload.get("mimeType", "")).startswith("multipart/"):
        children = payload.get("parts") or []
        if len(children) > 30:
            return None
        # 代替表現だけを1つ選ぶ。mixed/relatedの独立した本文は省略しない。
        if payload.get('mimeType') == 'multipart/alternative':
            preferred = next((part for part in children if _has_plain(part)), None)
            if preferred is not None:
                return _plain_text(preferred)
            for part in children:
                text = _plain_text(part)
                if text is None or text:
                    return text
            return ''
        parts = [_plain_text(part) for part in children]
        if any(part is None for part in parts):
            return None
        text = "\n".join(part for part in parts if part)
        return text if within_limit(text) else None
    return ""


def within_limit(text: str) -> bool:
    """JavaScriptと同じUTF-16単位で、配送先に余裕のある4万文字を上限にする。"""
    return len(text.encode('utf-16-le')) // 2 <= LIMIT


def _has_plain(part):
    if part.get('filename') or part.get('mimeType') == 'message/rfc822':
        return False
    return part.get('mimeType') == 'text/plain' or any(_has_plain(p) for p in part.get('parts', []))


def validate_payload(payload):
    """GASから受け取る本文MIMEも同じ深さ・部品数・サイズ制限で検証する。"""
    remaining = [100, 250000]
    def visit(part, depth):
        if not isinstance(part, dict) or depth > 20:
            raise ValueError('本文の構造が不正です')
        remaining[0] -= 1
        if remaining[0] < 0:
            raise ValueError('本文の部品数が多すぎます')
        if part.get('filename') or part.get('mimeType') == 'message/rfc822':
            return
        body = part.get('body') or {}
        if not isinstance(body, dict) or not isinstance(part.get('mimeType', ''), str):
            raise ValueError('本文の型が不正です')
        data = body.get('data') or ''
        headers = part.get('headers') or []
        children = part.get('parts') or []
        if not isinstance(data, str) or not isinstance(headers, list) or not isinstance(children, list) or len(children) > 30 or len(headers) > 100:
            raise ValueError('本文の項目が不正です')
        if any(not isinstance(h, dict) or not isinstance(h.get('name'), str) or not isinstance(h.get('value'), str) for h in headers):
            raise ValueError('本文のヘッダーが不正です')
        remaining[1] -= len(data) + sum(len(h['name']) + len(h['value']) for h in headers)
        if remaining[1] < 0:
            raise ValueError('本文の取得容量を超えました')
        for child in children:
            visit(child, depth+1)
    visit(payload, 0)


def hydrate_body(payload, fetch):
    """本文の外部参照だけを取得する。添付ファイルや転送メールは辿らない。"""
    from copy import deepcopy
    try:
        validate_payload(payload)
    except (ValueError, TypeError):
        return {'mimeType': 'text/plain', 'body': {'data': ''}}
    result = deepcopy(payload)
    remaining = [4, 250000]
    def visit(part):
        if part.get('filename') or part.get('mimeType') == 'message/rfc822':
            return
        body = part.get('body') or {}
        if part.get('mimeType') in {'text/plain', 'text/html'} and not body.get('data') and body.get('attachmentId'):
            if remaining[0] <= 0 or not isinstance(body.get('size'), int) or not 0 < body['size'] <= 150000:
                return  # 欠落部分は本文抽出時に全文不明として保留する。
            remaining[0] -= 1
            data = fetch(body['attachmentId'])
            encoded = data.get('data')
            if not isinstance(encoded, str) or len(encoded) > remaining[1]:
                raise ValueError('本文取得の容量を超えました')
            remaining[1] -= len(encoded)
            part['body'] = {**body, 'data': encoded}
        for child in part.get('parts') or []:
            visit(child)
    try:
        visit(result)
    except ValueError:
        return {'mimeType': 'text/plain', 'body': {'data': ''}}
    return result
