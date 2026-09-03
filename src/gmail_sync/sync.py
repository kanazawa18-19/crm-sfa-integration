"""Gmail同期の本体(2026-08-16)。

Zoho CRM方式(メアド一致による自動関連付け)を採用: 特定の連絡先を先に知っている必要は
なく、まず営業担当ごとに直近のメール全部を取得し、各メッセージの送信者/宛先を連絡先DB
(メールアドレス)と突き合わせる。

同期経路は2つある(2026-08-16、Gmail Push通知対応):
- `sync_all()`: `GET /api/cron/gmail-sync`(Vercel Cron、1日1回)から呼ばれる、直近2日分を
  毎回スキャンするフル同期。Push通知が主経路になった後も、Push未登録の担当者への安全網、
  および何らかの理由でPush通知を取りこぼした場合の見逃し防止の日次セーフティネットとして
  そのまま残す。
- `sync_rep_incremental()`: `gmail_push_webhook.py`(Pub/Sub Push通知)から呼ばれる、
  `historyId`起点の増分同期(主経路、ほぼリアルタイム)。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Callable

from src.audit_log.actor_context import set_actor
from src.db_schema.registry import get_schema
from src.gmail_sync import db, gmail_client
from src.gmail_sync.matcher import find_contact_page_id
from src.gmail_sync.notify import notify_web_engagement_tool
from src.gmail_sync.token_crypto import decrypt_token
from src.incident_detection.notify import notify_managers_immediate
from src.incident_detection.scorer import score_email
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

_CONTACT_DB_KEY = "contact"
_LAST_EMAIL_AT_PROPERTY = "最終メール日時"


def internal_domains_from_env() -> frozenset[str]:
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


@dataclass(frozen=True)
class ClassifiedMessage:
    """Gmailのメッセージ1件を連絡先DBと突き合わせた結果(2026-09-03に切り出し)。

    「どの連絡先の・どちら向きの・いつのメールか」を決めるところまでが責務で、
    `EmailLog`への記録・Notion更新・通知は含まない。日次同期(`_process_message_ref()`)と
    過去分の一括取り込み(`scripts/backfill_gmail_history.py`)の両方がここを通る。

    **突合とdirection判定を2箇所に書かないための分離。** 過去分の取り込みは副作用
    (インシデント通知・Notion更新・web-engagement-toolへの通知)を一切起こしてはならず、
    かといってロジックをコピーすると片方だけ直したときに静かにズレる。
    """

    contact_page_id: str
    contact_email: str
    direction: str  # "inbound" | "outbound"
    sent_at: datetime


def classify_message(
    message: gmail_client.GmailMessage,
    *,
    rep_email: str,
    internal_domains: frozenset[str],
    resolve_contact: Callable[[str], str | None],
) -> ClassifiedMessage | None:
    """メッセージ1件を連絡先DBと突き合わせる。記録対象でなければNoneを返す。

    `resolve_contact`はメールアドレス→連絡先ページIDの解決方法。日次同期はNotion APIへ
    1件ずつ問い合わせる関数を渡すが、過去分の一括取り込みでは**メール1通ごとにNotionを
    叩くとレート制限で現実的な時間に終わらない**ため、事前に作ったローカル辞書の
    `.get`を渡す。
    """
    from_addrs = _extract_addresses(message.from_header)
    to_addrs = _extract_addresses(message.to_header)

    rep_email_lower = rep_email.lower()
    # 社外(連絡先候補になりうる)アドレスのみに絞る。From/Toどちらに載っていたかは
    # 後段のdirection判定で使うため、setに潰さずFrom側を優先する順序で連結する。
    candidate_addrs = [
        a for a in (from_addrs + to_addrs) if a != rep_email_lower and a.rsplit("@", 1)[-1] not in internal_domains
    ]
    if not candidate_addrs:
        return None

    # 複数の社外アドレスが同時に載っているケース(CC等)は、連絡先DBで最初に一致した
    # 1件のみを対象とする — meeting_syncの「案件0件/複数件はスキップ」とは異なり、
    # メール自体は常に1通実在する事実であり、宛先が複数だからといって記録を
    # 見送るべき曖昧なケースではないため。
    for addr in candidate_addrs:
        contact_id = resolve_contact(addr)
        if contact_id:
            return ClassifiedMessage(
                contact_page_id=contact_id,
                contact_email=addr,
                direction="inbound" if addr in from_addrs else "outbound",
                sent_at=_parse_sent_at(message.date_header),
            )
    return None


def _process_message_ref(
    message_id: str,
    access_token: str,
    rep_email: str,
    contact_client: HttpNotionClient,
    *,
    internal_domains: frozenset[str],
) -> bool:
    """メッセージID1件分の処理(連絡先突合・EmailLog記録・Notion更新・Webhook通知)。

    `sync_rep()`(直近スキャン由来)・`sync_rep_incremental()`(history由来)の両方から
    共通で使う(重複実装を避けるための分割、2026-08-16)。新規に記録した場合Trueを返す。
    """
    if db.email_log_exists(message_id):
        return False

    message = gmail_client.get_message(access_token, message_id)
    classified = classify_message(
        message,
        rep_email=rep_email,
        internal_domains=internal_domains,
        resolve_contact=lambda addr: find_contact_page_id(contact_client, addr),
    )
    if classified is None:
        return False

    matched_contact_id = classified.contact_page_id
    matched_email = classified.contact_email
    direction = classified.direction
    sent_at = classified.sent_at

    # インシデント・アクシデント検知(2026-08-16、src/incident_detection/)。顧客からの
    # 受信メールのみを対象とする(outboundは自社側の発信文面であり検知対象外)。
    #
    # score_email()自体もtry/exceptで隔離する(shirokuma-secレビューWARN対応、2026-08-16):
    # keywords.pyは金沢さんが今後追記・修正しうる非エンジニア向けデータであり、正規表現の
    # 記述ミス等で例外が飛ぶと、インシデント検知という副次機能のバグでGmail同期という
    # 中核機能全体(EmailLog記録)が止まってしまう。失敗時は(None, None)にフォールバックして
    # メイン処理を継続させる(notify_managers_immediate()と同じ「副次機能は失敗してもメインを
    # 止めない」方針)。
    incident_score: int | None = None
    incident_priority: str | None = None
    if direction == "inbound":
        try:
            incident_score, incident_priority = score_email(message.subject, message.snippet)
        except Exception:
            logger.exception("gmail_sync: failed to score incident for %s", matched_email)

    db.insert_email_log(
        contact_page_id=matched_contact_id,
        contact_email=matched_email,
        rep_email=rep_email,
        gmail_message_id=message.id,
        direction=direction,
        subject=message.subject,
        snippet=message.snippet,
        sent_at=sent_at,
        incident_score=incident_score,
        incident_priority=incident_priority,
    )
    # rep_email（同期対象の営業担当）をactorLabelとして記録する（obasan-qualityレビュー
    # WARN対応、2026-08-17。db.insert_email_log()に既に渡している値と同じ）。
    with set_actor("gmail_sync", label=rep_email):
        contact_client.update_page(matched_contact_id, {_LAST_EMAIL_AT_PROPERTY: sent_at.isoformat()})

    if incident_priority == "high":
        # 副次通知は失敗してもメイン処理(EmailLog記録)に影響させない
        # (notify_web_engagement_toolと同じ方針。notify_managers_immediate自体も内部で
        # try/exceptしているが、ここでも隔離しておくことで呼び出し側の想定漏れに備える)。
        try:
            notify_managers_immediate(
                subject=message.subject,
                snippet=message.snippet,
                contact_email=matched_email,
                rep_email=rep_email,
                score=incident_score or 0,
            )
        except Exception:
            logger.exception("gmail_sync: failed to notify managers of incident for %s", matched_email)

    notify_web_engagement_tool(
        contact_email=matched_email,
        direction=direction,
        sent_at=sent_at,
        subject=message.subject,
        snippet=message.snippet,
        rep_email=rep_email,
    )

    return True


def _process_message_ref_or_skip(
    message_id: str,
    access_token: str,
    rep_email: str,
    contact_client: HttpNotionClient,
    *,
    internal_domains: frozenset[str],
) -> bool:
    """`_process_message_ref()`をHTTP 404(メッセージが既に恒久的に存在しない)についてのみ
    握りつぶしてスキップするラッパー(2026-08-26、本番障害の緊急バグ修正)。

    Gmail Push通知のhistoryイベントに載っているメッセージIDが、スパムとして完全削除・
    ユーザーによる完全削除等の理由で実体を取得できない(404)ケースは正常に起こりうる。
    このケースをここでcatchせず呼び出し元のループ外まで伝播させると、`sync_rep_incremental()`
    ではそのメッセージ以降が一切処理されないだけでなく、`db.update_history_id()`にも
    到達できず`historyId`カーソルが恒久的に固まる。次回以降のPush通知でも同じ
    メッセージIDへ再度ぶつかり、同じ404で無限に同期が止まり続ける
    (2026-08-25〜26、本番でPush通知処理が170回連続失敗し続けた原因)。

    404以外の例外(ネットワークエラー・認証エラー等、一時障害の可能性があるもの)は
    ここでは握りつぶさずそのまま伝播させる。一時障害まで握りつぶして`historyId`だけ
    進めてしまうと、本来リトライで拾えたはずのメッセージを二度と拾えなくなる
    (恒久的な見逃し)ため、404(=リトライしても絶対に直らないことが明確なケース)のみを
    対象とする。
    """
    try:
        return _process_message_ref(
            message_id, access_token, rep_email, contact_client, internal_domains=internal_domains
        )
    except gmail_client.GmailApiError as exc:
        if exc.status_code == 404:
            logger.info("gmail_sync: message %s no longer exists (404), skipping", message_id)
            return False
        raise


def sync_rep(
    rep_email: str,
    refresh_token: str,
    contact_client: HttpNotionClient,
    *,
    internal_domains: frozenset[str],
) -> int:
    """1名分の営業担当のGmailを同期する(直近`gmail_client._SEARCH_WINDOW_DAYS`日分の
    フルスキャン)。新規に記録したメール件数を返す。"""
    access_token = gmail_client.refresh_access_token(refresh_token)
    refs = gmail_client.list_recent_messages(access_token)

    logged_count = 0
    for ref in refs:
        if _process_message_ref_or_skip(
            ref.id, access_token, rep_email, contact_client, internal_domains=internal_domains
        ):
            logged_count += 1

    return logged_count


def sync_rep_incremental(
    rep_email: str,
    refresh_token: str,
    contact_client: HttpNotionClient,
    *,
    internal_domains: frozenset[str],
) -> int:
    """1名分の営業担当のGmailを、保存済みの`historyId`起点で増分同期する(2026-08-16、
    `gmail_push_webhook.py`から呼ばれる主経路)。新規に記録したメール件数を返す。

    `historyId`の更新は、増分同期が正常完了した場合(`list_history()`のレスポンス自体に
    含まれる`historyId`を使う)にのみ行う。以下2つのフォールバック経路では`historyId`を
    進めない(shirokuma-secレビューWARN対応、2026-08-16 — 誤って進めるとバックログを
    飛び越えて恒久的な見逃しにつながるため):
    - 保存済み`historyId`が無い(Push未登録・初回)場合: `sync_rep()`(フル同期)にフォール
      バックするのみ。以降の増分同期への移行は`watch_registration.register_or_renew_watch()`
      が初回登録時に設定する`historyId`に委ねる。
    - `gmail_client.HistoryIdExpiredError`(`startHistoryId`が古すぎて404)の場合:
      `sync_rep()`にフォールバックした上で、保存済みの(もう使えない)`historyId`をクリアする
      (`db.update_history_id(rep_email, None)`)。クリアすることで、次回の
      `register_or_renew_watch()`が「未設定」と判断し、有効な値へ再ブートストラップできる。

    メッセージ単位の処理は`_process_message_ref_or_skip()`経由で呼ぶこと(2026-08-26、
    本番障害の緊急バグ修正)。個別メッセージが404(既に削除済み等)で取得できない場合に
    それをcatchせずこのループの外まで伝播させると、以降のメッセージ処理だけでなく上記の
    `historyId`更新自体にも到達できなくなり、`historyId`カーソルが恒久的に固まって次回以降
    毎回同じ404で失敗し続ける(実際に2026-08-25〜26でPush通知が170回連続失敗した)。
    """
    conn = db.find_connection_by_email(rep_email)
    stored_history_id = conn.history_id if conn is not None else None

    access_token = gmail_client.refresh_access_token(refresh_token)

    if not stored_history_id:
        return sync_rep(rep_email, refresh_token, contact_client, internal_domains=internal_domains)

    try:
        result = gmail_client.list_history(access_token, stored_history_id)
    except gmail_client.HistoryIdExpiredError:
        logger.warning(
            "gmail_sync: historyId expired for rep %s, falling back to full sync", rep_email
        )
        logged_count = sync_rep(
            rep_email, refresh_token, contact_client, internal_domains=internal_domains
        )
        db.update_history_id(rep_email, None)
        return logged_count

    logged_count = 0
    for message_id in result.message_ids:
        if _process_message_ref_or_skip(
            message_id, access_token, rep_email, contact_client, internal_domains=internal_domains
        ):
            logged_count += 1

    if result.history_id:
        db.update_history_id(rep_email, result.history_id)
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
    internal_domains = internal_domains_from_env()

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
