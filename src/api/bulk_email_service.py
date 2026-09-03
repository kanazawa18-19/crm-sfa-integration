"""一斉配信プレビューと、送信根拠の一覧のユースケース（2026-09-03）。

Notion（連絡先）とPostgres（配信停止・送信根拠）から材料を集めて、`src/bulk_email/builder.py`の
純粋関数へ渡すだけの層。**判断はここに書かない** — 誰に送れるか・法定表示が
揃っているかの判断は全部`bulk_email/`側にあり、ここはI/Oと形式変換だけを持つ。

```
   routes/bulk_email.py  ── HTTP
            ▼
   ここ                  ── Notion / Postgres / 環境変数を集める
            ▼
   bulk_email/builder.py ── 判断（外部I/O無し・テストが速い）
```
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from src.api.client_360_service import Client360DataSource
from src.bulk_email import compliance, db, unsubscribe
from src.bulk_email.audience import Contact, normalize_email
from src.bulk_email.consent import (
    BASIS_DESCRIPTIONS,
    BASIS_EVIDENCE_HINTS,
    BASIS_LABELS,
    STALE_LABEL,
    ConsentIndex,
    evaluate,
)
from src.bulk_email.ids import normalize_page_id
from src.bulk_email.builder import BuildResult, RenderedMessage, build_messages
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


def _to_contacts(
    client_page_id: str, client_name: str, records: Sequence[dict[str, Any]]
) -> list[Contact]:
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
                client_page_id=client_page_id,
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
        contacts.extend(_to_contacts(client_id, client_name, result.get("contacts") or []))
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


def to_dict(result: BuildResult) -> dict[str, Any]:
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
    preference_reader: Any = None,
) -> dict[str, Any]:
    """一斉配信のプレビューを組み立てて画面向けのdictで返す。

    `preference_reader`は`fetch_opt_outs(page_ids, emails)`と
    `fetch_consents(page_ids, emails)`を持つオブジェクト（既定は`src/bulk_email/db.py`）。
    テストで差し替えるための注入口。

    **どちらも握り潰さない** — 読めなかったときは例外を上げ、プレビュー自体を
    失敗させる。配信停止を0人として続けると止めた相手に送る形になり、
    根拠を0件として続けると「設定漏れなのかDB障害なのか」が画面から区別できなくなる。
    """
    source = data_source or Client360DataSource()
    reader = preference_reader or db

    unique_client_ids = list(dict.fromkeys(cid for cid in client_page_ids if (cid or "").strip()))
    if len(unique_client_ids) > MAX_CLIENTS_PER_PREVIEW:
        raise ValueError(
            f"一度に選べる取引先は{MAX_CLIENTS_PER_PREVIEW}社までです"
            f"（{len(unique_client_ids)}社が指定されました）。"
        )

    contacts, truncated, not_found = _collect_contacts(unique_client_ids, source)
    if not_found:
        logger.warning("bulk_email preview: 取引先が見つかりませんでした: %s", not_found)

    contact_page_ids = [contact.page_id for contact in contacts]
    contact_emails = [contact.email or "" for contact in contacts]
    opted_out_ids, opted_out_emails = reader.fetch_opt_outs(contact_page_ids, contact_emails)
    consents = reader.fetch_consents(contact_page_ids)

    result = build_messages(
        subject=subject,
        body=body,
        contacts=contacts,
        sender_name=sender_name,
        identity=compliance.load_sender_identity(),
        unsubscribe_secret=unsubscribe.load_secret(),
        unsubscribe_base_url=unsubscribe_base_url(),
        opted_out_page_ids=opted_out_ids,
        opted_out_emails=opted_out_emails,
        consents=consents,
        truncated_client_names=truncated,
    )

    payload = to_dict(result)
    if not_found:
        payload["warnings"].append(
            f"指定された取引先のうち{len(not_found)}件がNotionで見つかりませんでした。"
        )
    return payload


def _consent_dict(index: ConsentIndex, contact: Contact) -> dict[str, Any]:
    """1連絡先ぶんの、今の根拠と判定結果。"""
    email = normalize_email(contact.email)
    record = index.find(contact.page_id)
    decision = evaluate(record, contact_email=email)
    return {
        "basis": record.basis if record else "",
        "basis_label": record.basis_label if record else "",
        "obtained_at": record.obtained_at.isoformat() if record and record.obtained_at else "",
        "evidence": record.evidence if record else "",
        "recorded_by": record.recorded_by if record else "",
        "recorded_email": record.contact_email if record else "",
        "revoked_at": record.revoked_at.date().isoformat()
        if record and record.revoked_at
        else "",
        "allowed": decision.allowed,
        "reason": decision.reason,
        "reason_label": decision.reason_label,
        "detail": decision.detail,
        "stale": decision.stale,
        # 「3年以上前」の文言はバックエンドで作る。しきい値を変えたときに
        # 画面の文言だけ嘘になるのを防ぐ。
        "stale_label": STALE_LABEL,
    }


def build_consent_overview(
    *,
    client_page_ids: Sequence[str],
    data_source: Client360DataSource | None = None,
    preference_reader: Any = None,
) -> dict[str, Any]:
    """選んだ取引先の連絡先と、その「送ってよい根拠」の今の状態を返す。

    登録画面（`dashboard/app/(dashboard)/bulk-email/consent/`）が使う。
    **ここも判断はしない** — 有効かどうかは`src/bulk_email/consent.py`が決める。

    配信停止の申し出がある相手も**隠さずに返す**（`unsubscribed`で示す）。
    画面から消してしまうと「なぜこの人だけ出てこないのか」が分からなくなるため。
    根拠を登録しても、配信停止が優先されて送られないことは変わらない。
    """
    source = data_source or Client360DataSource()
    reader = preference_reader or db

    unique_client_ids = list(dict.fromkeys(cid for cid in client_page_ids if (cid or "").strip()))
    if len(unique_client_ids) > MAX_CLIENTS_PER_PREVIEW:
        raise ValueError(
            f"一度に選べる取引先は{MAX_CLIENTS_PER_PREVIEW}社までです"
            f"（{len(unique_client_ids)}社が指定されました）。"
        )

    contacts, truncated, not_found = _collect_contacts(unique_client_ids, source)
    contact_page_ids = [contact.page_id for contact in contacts]
    contact_emails = [contact.email or "" for contact in contacts]
    opted_out_ids, opted_out_emails = reader.fetch_opt_outs(contact_page_ids, contact_emails)
    index = ConsentIndex(reader.fetch_consents(contact_page_ids))

    opted_out_id_set = {normalize_page_id(page_id) for page_id in opted_out_ids}
    opted_out_email_set = {normalize_email(email) for email in opted_out_emails}

    rows: list[dict[str, Any]] = []
    for contact in contacts:
        email = normalize_email(contact.email)
        rows.append(
            {
                "contact_page_id": contact.page_id,
                "contact_name": contact.name,
                "client_name": contact.client_name,
                # 登録時にどの取引先の下で確かめるか。画面が取引先名で逆引きすると
                # 同名の取引先で取り違える（3体が独立に指摘、2026-09-03）。
                "client_page_id": contact.client_page_id,
                "department": contact.department or "",
                "title": contact.title or "",
                "email": email,
                "unsubscribed": normalize_page_id(contact.page_id) in opted_out_id_set
                or (bool(email) and email in opted_out_email_set),
                "consent": _consent_dict(index, contact),
            }
        )

    warnings: list[str] = []
    if truncated:
        warnings.append(
            "連絡先が多く、次の取引先は先頭までしか読み込めていません: " + "・".join(truncated)
        )
    if not_found:
        warnings.append(
            f"指定された取引先のうち{len(not_found)}件がNotionで見つかりませんでした。"
        )

    return {
        "contacts": rows,
        "warnings": warnings,
        "basis_options": [
            {
                "value": value,
                "label": label,
                "description": BASIS_DESCRIPTIONS.get(value, ""),
                "evidence_hint": BASIS_EVIDENCE_HINTS.get(value, ""),
            }
            for value, label in BASIS_LABELS.items()
        ],
        "counts": {
            "total": len(rows),
            "allowed": sum(1 for row in rows if row["consent"]["allowed"]),
            "unsubscribed": sum(1 for row in rows if row["unsubscribed"]),
        },
    }
