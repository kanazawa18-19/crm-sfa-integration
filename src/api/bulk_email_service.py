"""一斉配信プレビューのユースケース（2026-09-03）。

Notion（連絡先）とPostgres（配信停止）から材料を集めて、`src/bulk_email/preview.py`の
純粋関数へ渡すだけの層。**判断はここに書かない** — 誰に送れるか・法定表示が
揃っているかの判断は全部`bulk_email/`側にあり、ここはI/Oと形式変換だけを持つ。

```
   routes/bulk_email.py  ── HTTP
            ▼
   ここ                  ── Notion / Postgres / 環境変数を集める
            ▼
   bulk_email/preview.py ── 判断（外部I/O無し・テストが速い）
```
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from src.api.client_360_service import Client360DataSource
from src.bulk_email import compliance, db, unsubscribe
from src.bulk_email.audience import Contact
from src.bulk_email.preview import PreviewResult, RenderedMessage, build_preview
from src.bulk_email.template import PLACEHOLDERS

logger = logging.getLogger(__name__)

# 1回のプレビューで選べる取引先の上限。Notionへの問い合わせが取引先数だけ増えるため、
# 画面から無制限に投げられないようにする（Vercelの実行時間上限に当てないための保険でもある）。
MAX_CLIENTS_PER_PREVIEW = 20

_PROP_名前 = "名前"
_PROP_部署 = "部署"
_PROP_役職 = "役職"
_PROP_メールアドレス = "メールアドレス"


def _text(value: Any) -> str | None:
    """Notionの表示用dictの値を文字列にする（未入力はNone）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def unsubscribe_base_url() -> str:
    """配信停止ページ（ダッシュボード側）のURL。

    専用の`DASHBOARD_BASE_URL`を優先し、無ければCORS設定に入っている
    `DASHBOARD_FRONTEND_ORIGIN`の先頭を使う（本番では同じ値になる。設定を
    2箇所に書かせないための後方互換であって、`DASHBOARD_BASE_URL`を設定するのが本筋）。
    """
    explicit = (os.environ.get("DASHBOARD_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    origins = (os.environ.get("DASHBOARD_FRONTEND_ORIGIN") or "").split(",")
    for origin in origins:
        if origin.strip():
            return origin.strip().rstrip("/")
    return ""


def _to_contacts(client_name: str, records: Sequence[dict[str, Any]]) -> list[Contact]:
    contacts: list[Contact] = []
    for record in records:
        page_id = record.get("notion_page_id")
        if not page_id:
            continue
        contacts.append(
            Contact(
                page_id=str(page_id),
                name=_text(record.get(_PROP_名前)) or "",
                email=_text(record.get(_PROP_メールアドレス)),
                department=_text(record.get(_PROP_部署)),
                title=_text(record.get(_PROP_役職)),
                client_name=client_name,
            )
        )
    return contacts


def _collect_contacts(
    client_page_ids: Sequence[str], data_source: Client360DataSource
) -> tuple[list[Contact], list[str], list[str]]:
    """選ばれた取引先の連絡先を集める。

    戻り値は（連絡先、連絡先が打ち切られた取引先名、見つからなかった取引先ID）。
    """
    contacts: list[Contact] = []
    truncated: list[str] = []
    not_found: list[str] = []
    for client_id in client_page_ids:
        result = data_source.fetch_client_contacts(client_id)
        if result is None:
            not_found.append(client_id)
            continue
        client_name = str(result.get("client_name") or "")
        contacts.extend(_to_contacts(client_name, result.get("contacts") or []))
        if result.get("truncated"):
            truncated.append(client_name or client_id)
    return contacts, truncated, not_found


def _message_dict(message: RenderedMessage) -> dict[str, Any]:
    return {
        "contact_page_id": message.contact_page_id,
        "contact_name": message.contact_name,
        "client_name": message.client_name,
        "to_email": message.to_email,
        "subject": message.subject,
        "body": message.body,
    }


def to_dict(result: PreviewResult) -> dict[str, Any]:
    return {
        "sendable": result.sendable,
        "blockers": list(result.blockers),
        "warnings": list(result.warnings),
        "placeholders_used": list(result.placeholders_used),
        "placeholders_available": [
            {"name": name, "description": description}
            for name, description in PLACEHOLDERS.items()
        ],
        "messages": [_message_dict(message) for message in result.messages],
        "skipped": [
            {
                "contact_page_id": item.contact.page_id,
                "contact_name": item.contact.name,
                "client_name": item.contact.client_name,
                "email": item.contact.email or "",
                "reason": item.reason,
                "reason_label": item.reason_label,
                "detail": item.detail,
            }
            for item in result.skipped
        ],
        "counts": {
            "sendable": len(result.messages),
            "skipped": len(result.skipped),
        },
    }


def build_bulk_email_preview(
    *,
    subject: str,
    body: str,
    client_page_ids: Sequence[str],
    sender_name: str = "",
    data_source: Client360DataSource | None = None,
    opt_out_reader: Any = None,
) -> dict[str, Any]:
    """一斉配信のプレビューを組み立てて画面向けのdictで返す。

    `opt_out_reader`は`fetch_opt_outs(page_ids, emails)`を持つオブジェクト
    （既定は`src/bulk_email/db.py`）。テストで差し替えるための注入口。
    **既定のまま握り潰さない** — 配信停止が読めなかったときは例外を上げ、
    プレビュー自体を失敗させる（0人として続けると、止めた相手に送る形になる）。
    """
    source = data_source or Client360DataSource()
    reader = opt_out_reader or db

    unique_client_ids = list(dict.fromkeys(cid for cid in client_page_ids if (cid or "").strip()))
    if len(unique_client_ids) > MAX_CLIENTS_PER_PREVIEW:
        raise ValueError(
            f"一度に選べる取引先は{MAX_CLIENTS_PER_PREVIEW}社までです"
            f"（{len(unique_client_ids)}社が指定されました）。"
        )

    contacts, truncated, not_found = _collect_contacts(unique_client_ids, source)
    if not_found:
        logger.warning("bulk_email preview: 取引先が見つかりませんでした: %s", not_found)

    opted_out_ids, opted_out_emails = reader.fetch_opt_outs(
        [contact.page_id for contact in contacts],
        [contact.email or "" for contact in contacts],
    )

    result = build_preview(
        subject=subject,
        body=body,
        contacts=contacts,
        sender_name=sender_name,
        identity=compliance.load_sender_identity(),
        unsubscribe_secret=unsubscribe.load_secret(),
        unsubscribe_base_url=unsubscribe_base_url(),
        opted_out_page_ids=opted_out_ids,
        opted_out_emails=opted_out_emails,
        truncated_client_names=truncated,
    )

    payload = to_dict(result)
    if not_found:
        payload["warnings"].append(
            f"指定された取引先のうち{len(not_found)}件がNotionで見つかりませんでした。"
        )
    return payload
