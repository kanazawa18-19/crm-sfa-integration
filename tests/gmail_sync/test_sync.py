from __future__ import annotations

from datetime import datetime, timezone

from src.gmail_sync import db, sync
from src.gmail_sync.gmail_client import GmailMessage, GmailMessageRef, HistoryIdExpiredError


def test_extract_addresses_parses_name_and_plain_forms() -> None:
    assert sync._extract_addresses("Taro Yamada <taro@example.com>") == ["taro@example.com"]
    assert sync._extract_addresses("a@example.com, Name <b@example.com>") == [
        "a@example.com",
        "b@example.com",
    ]


def test_extract_addresses_lowercases() -> None:
    assert sync._extract_addresses("Taro@Example.COM") == ["taro@example.com"]


def test_parse_sent_at_valid_header() -> None:
    result = sync._parse_sent_at("Mon, 16 Aug 2026 09:00:00 +0900")
    assert result.year == 2026
    assert result.month == 8
    assert result.day == 16


def test_parse_sent_at_missing_header_falls_back_to_now() -> None:
    before = datetime.now(timezone.utc)
    result = sync._parse_sent_at(None)
    after = datetime.now(timezone.utc)
    assert before <= result <= after


def test_parse_sent_at_malformed_header_falls_back_to_now() -> None:
    before = datetime.now(timezone.utc)
    result = sync._parse_sent_at("not a date")
    after = datetime.now(timezone.utc)
    assert before <= result <= after


class FakeContactClient:
    def __init__(self, contacts_by_email: dict[str, str]) -> None:
        self._by_email = contacts_by_email
        self.updated_pages: list[tuple[str, dict]] = []

    def update_page(self, page_id: str, properties: dict) -> None:
        self.updated_pages.append((page_id, properties))


def _message(
    id_: str = "msg1",
    from_header: str = "lead@client.example.com",
    to_header: str = "rep@cnctor.jp",
    subject: str | None = "件名",
    date_header: str | None = "Mon, 16 Aug 2026 09:00:00 +0900",
    snippet: str | None = "本文の抜粋",
) -> GmailMessage:
    return GmailMessage(
        id=id_,
        from_header=from_header,
        to_header=to_header,
        subject=subject,
        date_header=date_header,
        snippet=snippet,
    )


def test_sync_rep_logs_inbound_email_when_sender_matches_contact(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(sync.gmail_client, "get_message", lambda access_token, message_id: _message())
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))

    def fake_find_contact_page_id(client, email):
        return "contact-page-1" if email == "lead@client.example.com" else None

    monkeypatch.setattr(sync, "find_contact_page_id", fake_find_contact_page_id)

    notified: list[dict] = []
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: notified.append(kwargs))

    contact_client = FakeContactClient({})
    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", contact_client, internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert len(inserted) == 1
    assert inserted[0]["direction"] == "inbound"
    assert inserted[0]["contact_email"] == "lead@client.example.com"
    assert inserted[0]["contact_page_id"] == "contact-page-1"
    assert len(contact_client.updated_pages) == 1
    assert contact_client.updated_pages[0][0] == "contact-page-1"
    assert len(notified) == 1
    assert notified[0]["direction"] == "inbound"


def test_sync_rep_logs_outbound_email_when_recipient_matches_contact(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: _message(from_header="rep@cnctor.jp", to_header="lead@client.example.com"),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))
    monkeypatch.setattr(
        sync, "find_contact_page_id", lambda client, email: "contact-page-1" if email == "lead@client.example.com" else None
    )
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert inserted[0]["direction"] == "outbound"


def test_sync_rep_skips_already_logged_messages(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: True)

    def fail_get_message(*args, **kwargs):
        raise AssertionError("get_message should not be called for already-logged messages")

    monkeypatch.setattr(sync.gmail_client, "get_message", fail_get_message)

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )
    assert count == 0


def test_sync_rep_skips_messages_with_no_matching_contact(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(sync.gmail_client, "get_message", lambda access_token, message_id: _message())
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: None)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )
    assert count == 0
    assert inserted == []


def test_sync_rep_skips_messages_between_only_internal_addresses(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: _message(from_header="rep@cnctor.jp", to_header="colleague@cnctor.jp"),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    def fail_find_contact(*args, **kwargs):
        raise AssertionError("find_contact_page_id should not be called when no external address is present")

    monkeypatch.setattr(sync, "find_contact_page_id", fail_find_contact)

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )
    assert count == 0


# --- sync_rep_incremental (2026-08-16、Gmail Push通知対応) ------------------------------------
#
# shirokuma-secレビューWARN対応(2026-08-16): historyIdの更新は増分同期(list_history())が
# 正常完了した場合のみ行う。フォールバック経路(historyId未保存・期限切れ)では、
# get_profile()等で"現在の"historyIdを取得して上書きしない(バックログを飛び越えた恒久的な
# 見逃しにつながるため)。期限切れの場合は保存済みのhistoryIdをNoneへクリアし、次回の
# watch登録(register_or_renew_watch())で再ブートストラップできるようにするに留める。


def _stored_connection(history_id: str | None) -> db.RepGmailConnection:
    return db.RepGmailConnection(
        rep_email="rep@cnctor.jp",
        refresh_token_enc="enc",
        last_synced_at=None,
        history_id=history_id,
        watch_expiration=None,
    )


def test_sync_rep_incremental_falls_back_to_full_sync_when_no_stored_history_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection(None))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(sync.gmail_client, "get_message", lambda access_token, message_id: _message())
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: None)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    def fail_update_history_id(*args, **kwargs):
        raise AssertionError("update_history_id should not be called on the no-stored-id fallback path")

    monkeypatch.setattr(sync.db, "update_history_id", fail_update_history_id)

    def fail_list_history(*args, **kwargs):
        raise AssertionError("list_history should not be called when no historyId is stored")

    monkeypatch.setattr(sync.gmail_client, "list_history", fail_list_history)

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1


def test_sync_rep_incremental_uses_list_history_when_history_id_present(monkeypatch) -> None:
    from src.gmail_sync.gmail_client import HistoryListResult

    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("1000"))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_history",
        lambda access_token, start_history_id: HistoryListResult(message_ids=["msg1"], history_id="6000"),
    )
    monkeypatch.setattr(sync.gmail_client, "get_message", lambda access_token, message_id: _message())
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    saved: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        sync.db, "update_history_id", lambda rep_email, history_id: saved.append((rep_email, history_id))
    )

    def fail_list_recent_messages(*args, **kwargs):
        raise AssertionError("list_recent_messages should not be called during incremental sync")

    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", fail_list_recent_messages)

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert len(inserted) == 1
    # list_history()自体のレスポンス由来のhistoryIdをそのまま使う(get_profile()は呼ばない)。
    assert saved == [("rep@cnctor.jp", "6000")]


def test_sync_rep_incremental_does_not_update_history_id_when_response_omits_it(monkeypatch) -> None:
    from src.gmail_sync.gmail_client import HistoryListResult

    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("1000"))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_history",
        lambda access_token, start_history_id: HistoryListResult(message_ids=[], history_id=None),
    )

    def fail_update_history_id(*args, **kwargs):
        raise AssertionError("update_history_id should not be called when list_history() omits historyId")

    monkeypatch.setattr(sync.db, "update_history_id", fail_update_history_id)

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 0


def test_sync_rep_incremental_falls_back_to_full_sync_and_clears_history_id_when_expired(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("too-old"))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")

    def raise_expired(access_token, start_history_id):
        raise HistoryIdExpiredError(404, "not found")

    monkeypatch.setattr(sync.gmail_client, "list_history", raise_expired)
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(sync.gmail_client, "get_message", lambda access_token, message_id: _message())
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: None)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    saved: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        sync.db, "update_history_id", lambda rep_email, history_id: saved.append((rep_email, history_id))
    )

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    # 期限切れの古いhistoryIdを「今」の値で上書きするのではなくNoneへクリアする
    # (次回のwatch登録で再ブートストラップできるようにするため)。
    assert saved == [("rep@cnctor.jp", None)]
