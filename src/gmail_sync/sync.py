"""Gmail同期の本体(2026-08-16)。cron(src/api/app.pyの新規エンドポイント)から呼ばれる想定。

Zoho CRM方式(メアド一致による自動関連付け)を採用: 特定の連絡先を先に知っている必要は
なく、まず営業担当ごとに直近のメール全部(`gmail_client.list_recent_messages`)を取得し、
各メッセージの送信者/宛先を連絡先DB(メールアドレス)と突き合わせる。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime

from src.db_schema.registry import get_schema
from src.gmail_sync import db, gmail_client
from src.gmail_sync.matcher import find_contact_page_id
from src.gmail_sync.notify import notify_web_engagement_tool
from src.gmail_sync.token_crypto import decrypt_token
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

_CONTACT_DB_KEY = "contact"
_LAST_EMAIL_AT_PROPERTY = "最終メール日時"


def _internal_domains() -> frozenset[str]:
    """web_engagement_meeting_webhook.pyの`_internal_domains()`と同じ
    INTERNAL_EMAIL_DOMAINS環境変数(カンマ区切り)を使う。"""
    raw = os.environ.get("INTERNAL_EMAIL_DOMAINS", "")
    return frozenset(domain.strip().lower() for domain in raw.split(",") if domain.strip())


def _extract_addresses(header_value: str) -> list[str]:
    """"Name <a@example.com>, Name2 <b@example.com>"形式のヘッダーからメールアドレスのみ抽出する。"""
    return [addr.lower() for _, addr in getaddresses([header_value]) if addr]


def _parse_sent_at(date_header: str | None) -> datetime:
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError, IndexError):
            logger.warning("gmail_sync: failed to parse Date header %r, falling back to now", date_header)
    return datetime.now(timezone.utc)


def sync_rep(
    rep_email: str,
    refresh_token: str,
    contact_client: HttpNotionClient,
    *,
    internal_domains: frozenset[str],
) -> int:
    """1名分の営業担当のGmailを同期する。新規に記録したメール件数を返す。"""
    access_token = gmail_client.refresh_access_token(refresh_token)
    refs = gmail_client.list_recent_messages(access_token)

    logged_count = 0
    for ref in refs:
        if db.email_log_exists(ref.id):
            continue

        message = gmail_client.get_message(access_token, ref.id)
        from_addrs = _extract_addresses(message.from_header)
        to_addrs = _extract_addresses(message.to_header)

        rep_email_lower = rep_email.lower()
        # 社外(連絡先候補になりうる)アドレスのみに絞る。From/Toどちらに載っていたかは
        # 後段のdirection判定で使うため、setに潰さずFrom側を優先する順序で連結する。
        candidate_addrs = [
            a for a in (from_addrs + to_addrs) if a != rep_email_lower and a.rsplit("@", 1)[-1] not in internal_domains
        ]
        if not candidate_addrs:
            continue

        # 複数の社外アドレスが同時に載っているケース(CC等)は、連絡先DBで最初に一致した
        # 1件のみを対象とする — meeting_syncの「案件0件/複数件はスキップ」とは異なり、
        # メール自体は常に1通実在する事実であり、宛先が複数だからといって記録を
        # 見送るべき曖昧なケースではないため。
        matched_contact_id: str | None = None
        matched_email: str | None = None
        for addr in candidate_addrs:
            contact_id = find_contact_page_id(contact_client, addr)
            if contact_id:
                matched_contact_id, matched_email = contact_id, addr
                break
        if not matched_contact_id or not matched_email:
            continue

        direction = "inbound" if matched_email in from_addrs else "outbound"
        sent_at = _parse_sent_at(message.date_header)

        db.insert_email_log(
            contact_page_id=matched_contact_id,
            contact_email=matched_email,
            rep_email=rep_email,
            gmail_message_id=message.id,
            direction=direction,
            subject=message.subject,
            snippet=message.snippet,
            sent_at=sent_at,
        )
        contact_client.update_page(matched_contact_id, {_LAST_EMAIL_AT_PROPERTY: sent_at.isoformat()})
        logged_count += 1

        notify_web_engagement_tool(
            contact_email=matched_email,
            direction=direction,
            sent_at=sent_at,
            subject=message.subject,
            snippet=message.snippet,
            rep_email=rep_email,
        )

    return logged_count


def _default_contact_client() -> HttpNotionClient:
    schema = get_schema(_CONTACT_DB_KEY)
    return HttpNotionClient(_CONTACT_DB_KEY, schema.notion_database_id)


def sync_all(*, contact_client: HttpNotionClient | None = None) -> dict[str, int]:
    """Gmail連携済みの全営業担当を同期する。{rep_email: 記録件数(失敗時-1)}を返す。

    1名の同期失敗が他の担当の同期を止めないよう、担当ごとにtry/exceptで独立させる
    (syncAllGmail()のweb-engagement-tool側実装と同じ方針)。
    """
    client = contact_client or _default_contact_client()
    internal_domains = _internal_domains()

    results: dict[str, int] = {}
    for conn in db.list_gmail_connections():
        try:
            refresh_token = decrypt_token(conn.refresh_token_enc)
            count = sync_rep(conn.rep_email, refresh_token, client, internal_domains=internal_domains)
            results[conn.rep_email] = count
            db.update_last_synced_at(conn.rep_email, datetime.now(timezone.utc))
        except Exception:
            logger.exception("gmail_sync: failed to sync rep %s", conn.rep_email)
            results[conn.rep_email] = -1
    return results
