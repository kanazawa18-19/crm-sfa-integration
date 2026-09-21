from __future__ import annotations

from datetime import datetime, timezone

from src.gmail_sync import db, sync
from src.gmail_sync.gmail_client import GmailApiError, GmailMessage, GmailMessageRef, HistoryIdExpiredError


def test_extract_addresses_parses_name_and_plain_forms() -> None:
    assert sync._extract_addresses("Taro Yamada <taro@example.com>") == ["taro@example.com"]
    assert sync._extract_addresses("a@example.com, Name <b@example.com>") == [
        "a@example.com",
        "b@example.com",
    ]


def test_extract_addresses_lowercases() -> None:
    assert sync._extract_addresses("Taro@Example.COM") == ["taro@example.com"]


def _msg_for_sent_at(*, date_header=None, internal_date_ms=None) -> GmailMessage:
    return GmailMessage(
        id="m1",
        from_header="a@example.com",
        to_header="b@example.com",
        subject=None,
        date_header=date_header,
        snippet=None,
        internal_date_ms=internal_date_ms,
    )


def test_parse_sent_at_valid_header() -> None:
    result = sync._parse_sent_at(_msg_for_sent_at(date_header="Mon, 16 Aug 2026 09:00:00 +0900"))
    assert result.year == 2026
    assert result.month == 8
    assert result.day == 16


def test_parse_sent_at_prefers_internal_date_over_the_date_header() -> None:
    """Gmailが記録した時刻を優先する(送信側申告のDate:は時計ずれで動く)。"""
    # 2026-08-16 00:00:00 UTC
    result = sync._parse_sent_at(
        _msg_for_sent_at(
            date_header="Mon, 16 Aug 2026 09:00:00 +0900",
            internal_date_ms=str(int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() * 1000)),
        )
    )
    assert result == datetime(2026, 8, 20, tzinfo=timezone.utc)


def test_parse_sent_at_falls_back_to_the_date_header_when_internal_date_is_broken() -> None:
    result = sync._parse_sent_at(
        _msg_for_sent_at(date_header="Mon, 16 Aug 2026 09:00:00 +0900", internal_date_ms="abc")
    )
    assert result.day == 16


def test_parse_sent_at_missing_header_falls_back_to_now() -> None:
    before = datetime.now(timezone.utc)
    result = sync._parse_sent_at(_msg_for_sent_at())
    after = datetime.now(timezone.utc)
    assert before <= result <= after


def test_parse_sent_at_malformed_header_falls_back_to_now() -> None:
    before = datetime.now(timezone.utc)
    result = sync._parse_sent_at(_msg_for_sent_at(date_header="not a date"))
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


# --- 404(メッセージ削除済み)スキップ(2026-08-26、本番障害バグ修正) ----------------------------
#
# gmail_push_webhookが本番で170回連続失敗し続けていたバグの再現・修正確認。
# get_message()がGmailApiError(404)を送出しても、そのメッセージだけスキップして
# 他のメッセージの処理・historyIdの前進が継続することを検証する。


def test_sync_rep_incremental_skips_404_message_and_still_advances_history_id(
    monkeypatch,
) -> None:
    from src.gmail_sync.gmail_client import HistoryListResult

    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("1000"))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_history",
        lambda access_token, start_history_id: HistoryListResult(
            message_ids=["deleted-msg", "msg1"], history_id="6000"
        ),
    )

    def fake_get_message(access_token, message_id):
        if message_id == "deleted-msg":
            raise GmailApiError(404, "Requested entity was not found.")
        return _message(id_=message_id)

    monkeypatch.setattr(sync.gmail_client, "get_message", fake_get_message)
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    saved: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        sync.db, "update_history_id", lambda rep_email, history_id: saved.append((rep_email, history_id))
    )

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    # 削除済みメッセージ(404)はスキップされるが、後続のmsg1は処理される
    assert count == 1
    assert len(inserted) == 1
    assert inserted[0]["gmail_message_id"] == "msg1"
    # 404で1件失敗してもhistoryIdは前進する(本番バグ: これができずカーソルが固まっていた)
    assert saved == [("rep@cnctor.jp", "6000")]


def test_sync_rep_incremental_propagates_non_404_error_and_does_not_advance_history_id(
    monkeypatch,
) -> None:
    from src.gmail_sync.gmail_client import HistoryListResult

    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("1000"))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_history",
        lambda access_token, start_history_id: HistoryListResult(message_ids=["msg1"], history_id="6000"),
    )

    def raise_server_error(access_token, message_id):
        raise GmailApiError(500, "internal error")

    monkeypatch.setattr(sync.gmail_client, "get_message", raise_server_error)
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    def fail_update_history_id(*args, **kwargs):
        raise AssertionError("update_history_id should not be called when a non-404 error propagates")

    monkeypatch.setattr(sync.db, "update_history_id", fail_update_history_id)

    try:
        sync.sync_rep_incremental(
            "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
        )
        raised = False
    except GmailApiError:
        raised = True

    # 一時障害の可能性がある非404エラーは握りつぶさず伝播させ、historyIdも進めない
    # (安易に握りつぶすとリトライされるはずのメッセージを恒久的に見逃すため)
    assert raised


def test_sync_rep_skips_404_message_and_continues_with_others(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_recent_messages",
        lambda access_token: [GmailMessageRef(id="deleted-msg"), GmailMessageRef(id="msg1")],
    )

    def fake_get_message(access_token, message_id):
        if message_id == "deleted-msg":
            raise GmailApiError(404, "Requested entity was not found.")
        return _message(id_=message_id)

    monkeypatch.setattr(sync.gmail_client, "get_message", fake_get_message)
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert len(inserted) == 1
    assert inserted[0]["gmail_message_id"] == "msg1"


# --- インシデント・アクシデント検知連携(2026-08-16、src/incident_detection/) --------------------


def test_sync_rep_stores_incident_classification_for_inbound_email(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: _message(subject="ご連絡", snippet="不具合が発生しています"),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))

    notified: list[dict] = []
    monkeypatch.setattr(sync, "notify_managers_immediate", lambda **kwargs: notified.append(kwargs))

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert inserted[0]["incident_score"] == 3
    assert inserted[0]["incident_priority"] == "low"
    # low優先度は即時通知の対象外
    assert notified == []


def test_sync_rep_does_not_score_outbound_email(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: _message(
            from_header="rep@cnctor.jp",
            to_header="lead@client.example.com",
            subject="経緯報告書",
            snippet="不具合が発生しています",
        ),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))

    notified: list[dict] = []
    monkeypatch.setattr(sync, "notify_managers_immediate", lambda **kwargs: notified.append(kwargs))

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert inserted[0]["direction"] == "outbound"
    assert inserted[0]["incident_score"] is None
    assert inserted[0]["incident_priority"] is None
    assert notified == []


def test_sync_rep_notifies_managers_immediately_for_high_priority_incident(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: _message(subject="経緯報告書", snippet="不具合が発生しました"),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: None)

    notified: list[dict] = []
    monkeypatch.setattr(sync, "notify_managers_immediate", lambda **kwargs: notified.append(kwargs))

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert len(notified) == 1
    assert notified[0]["score"] == 8
    assert notified[0]["contact_email"] == "lead@client.example.com"
    assert notified[0]["rep_email"] == "rep@cnctor.jp"


def test_sync_rep_continues_when_notify_managers_immediate_raises(monkeypatch) -> None:
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: _message(subject="経緯報告書", snippet="不具合が発生しました"),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))

    def raise_error(**kwargs):
        raise RuntimeError("slack down")

    monkeypatch.setattr(sync, "notify_managers_immediate", raise_error)

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    # 通知失敗があってもメイン処理(EmailLog記録・戻り値の件数)は継続する
    assert count == 1
    assert len(inserted) == 1


def test_sync_rep_continues_with_none_classification_when_score_email_raises(monkeypatch) -> None:
    # keywords.pyは非エンジニア(金沢さん)が今後追記・修正しうるデータであり、正規表現の
    # 記述ミス等でscore_email()が例外を送出しても、Gmail同期という中核機能(EmailLog記録)は
    # 止めない(shirokuma-secレビューWARN対応、2026-08-16)。
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.gmail_client, "list_recent_messages", lambda access_token: [GmailMessageRef(id="msg1")])
    monkeypatch.setattr(sync.gmail_client, "get_message", lambda access_token, message_id: _message())
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    def raise_error(subject, snippet):
        raise RuntimeError("bad regex in keywords.py")

    monkeypatch.setattr(sync, "score_email", raise_error)

    inserted: list[dict] = []
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: inserted.append(kwargs))

    notified: list[dict] = []
    monkeypatch.setattr(sync, "notify_managers_immediate", lambda **kwargs: notified.append(kwargs))

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert count == 1
    assert len(inserted) == 1
    assert inserted[0]["incident_score"] is None
    assert inserted[0]["incident_priority"] is None
    assert notified == []


# --- classify_message（過去分の取り込みが使う経路、2026-09-03） -------------------------------


def _classify_target_message(*, msg_id: str = "m1", from_header: str, to_header: str) -> GmailMessage:
    return GmailMessage(
        id=msg_id,
        from_header=from_header,
        to_header=to_header,
        subject="件名",
        date_header="Tue, 01 Sep 2026 10:00:00 +0900",
        snippet="本文の先頭",
    )


def _classify(message, index: dict[str, str], *, internal=("cnctor.jp",)):
    """辞書の`.get`を`resolve_contact`に渡す経路（バックフィルと同じ使い方）。"""
    return sync.classify_message(
        message,
        rep_email="rep@cnctor.jp",
        internal_domains=frozenset(internal),
        resolve_contact=index.get,
    )


def test_classify_message_marks_mail_from_the_contact_as_inbound() -> None:
    result = _classify(
        _classify_target_message(from_header="Lead <lead@client.example.com>", to_header="rep@cnctor.jp"),
        {"lead@client.example.com": "cnt-1"},
    )

    assert result is not None
    assert result.direction == "inbound"
    assert result.contact_page_id == "cnt-1"
    assert result.contact_email == "lead@client.example.com"


def test_classify_message_marks_mail_to_the_contact_as_outbound() -> None:
    result = _classify(
        _classify_target_message(from_header="rep@cnctor.jp", to_header="Lead <lead@client.example.com>"),
        {"lead@client.example.com": "cnt-1"},
    )

    assert result is not None
    assert result.direction == "outbound"


def test_classify_message_returns_none_for_unknown_addresses() -> None:
    assert (
        _classify(
            _classify_target_message(from_header="stranger@example.org", to_header="rep@cnctor.jp"),
            {"lead@client.example.com": "cnt-1"},
        )
        is None
    )


def test_classify_message_ignores_internal_domains() -> None:
    assert (
        _classify(
            _classify_target_message(from_header="colleague@cnctor.jp", to_header="rep@cnctor.jp"),
            {"colleague@cnctor.jp": "cnt-9"},
        )
        is None
    )


def test_classify_message_prefers_the_sender_when_both_sides_are_known_contacts() -> None:
    """From側を先に見る（direction判定が安定するため）。"""
    result = _classify(
        _classify_target_message(
            from_header="Lead A <a@client.example.com>",
            to_header="rep@cnctor.jp, Lead B <b@client.example.com>",
        ),
        {"a@client.example.com": "cnt-a", "b@client.example.com": "cnt-b"},
    )

    assert result is not None
    assert result.contact_page_id == "cnt-a"
    assert result.direction == "inbound"


def test_classify_message_parses_the_date_header_into_utc() -> None:
    result = _classify(
        _classify_target_message(from_header="lead@client.example.com", to_header="rep@cnctor.jp"),
        {"lead@client.example.com": "cnt-1"},
    )

    assert result is not None
    # JST 10:00 = UTC 01:00
    assert result.sent_at.utcoffset().total_seconds() == 9 * 3600
    assert result.sent_at.astimezone(timezone.utc).hour == 1


def _incremental_fixture(monkeypatch, *, records: list[tuple[str, list[str]]], history_id: str = "9000"):
    """`sync_rep_incremental()`の時間予算テスト用の共通配線。`records`は
    (historyレコードid, そのレコードに載っているメッセージid...)の並び。"""
    from src.gmail_sync.gmail_client import HistoryListResult

    message_ids = [m for _, ids in records for m in ids]
    message_history_ids = {m: rid for rid, ids in records for m in ids}
    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("1000"))
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_history",
        lambda access_token, start_history_id: HistoryListResult(
            message_ids=message_ids, history_id=history_id, message_history_ids=message_history_ids
        ),
    )
    fetched: list[str] = []
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: fetched.append(message_id) or _message(id_=message_id),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: None)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)
    saved: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        sync.db, "update_history_id", lambda rep_email, history_id: saved.append((rep_email, history_id))
    )
    return fetched, saved


def _clock(monkeypatch, ticks: list[float]) -> None:
    """`time.monotonic()`を呼ぶたびに`ticks`を順に返す(尽きたら最後の値)。"""
    it = iter(ticks)
    last = ticks[-1]

    def fake_monotonic() -> float:
        nonlocal last
        last = next(it, last)
        return last

    monkeypatch.setattr(sync.time, "monotonic", fake_monotonic)


def test_sync_rep_incremental_stops_at_the_deadline_and_resumes_from_the_last_completed_record(
    monkeypatch,
) -> None:
    """レコードを処理し終えるたびにそのidを`historyId`へ保存し、期限を過ぎたら残りを打ち切る
    (2026-09-21)。レコード4002の途中で切れたので、保存されているのは4001まで。"""
    fetched, saved = _incremental_fixture(
        monkeypatch, records=[("4001", ["m1", "m2"]), ("4002", ["m3", "m4"]), ("4003", ["m5"])]
    )
    # 各メッセージの前に1回ずつ時計を見る: m1=0, m2=1, m3=2, m4=100(期限切れ)
    _clock(monkeypatch, [0.0, 1.0, 2.0, 100.0])

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp",
        "refresh-token",
        FakeContactClient({}),
        internal_domains=frozenset({"cnctor.jp"}),
        deadline=50.0,
    )

    assert fetched == ["m1", "m2", "m3"]
    assert count == 3
    # 最新の9000ではなく、処理し終えたレコードの4001まで進める。
    assert saved == [("rep@cnctor.jp", "4001")]


def test_sync_rep_incremental_does_not_advance_history_id_when_no_record_was_completed(
    monkeypatch,
) -> None:
    fetched, saved = _incremental_fixture(monkeypatch, records=[("4001", ["m1", "m2"])])
    _clock(monkeypatch, [0.0, 100.0])

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp",
        "refresh-token",
        FakeContactClient({}),
        internal_domains=frozenset({"cnctor.jp"}),
        deadline=50.0,
    )

    assert fetched == ["m1"]
    assert count == 1
    assert saved == []


def test_sync_rep_incremental_advances_to_the_latest_history_id_when_finished_within_the_deadline(
    monkeypatch,
) -> None:
    fetched, saved = _incremental_fixture(monkeypatch, records=[("4001", ["m1"]), ("4002", ["m2"])])
    _clock(monkeypatch, [0.0, 1.0])

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp",
        "refresh-token",
        FakeContactClient({}),
        internal_domains=frozenset({"cnctor.jp"}),
        deadline=50.0,
    )

    assert fetched == ["m1", "m2"]
    assert count == 2
    # レコードごとの保存 → 最後に応答全体の最新historyId。
    assert saved == [("rep@cnctor.jp", "4001"), ("rep@cnctor.jp", "4002"), ("rep@cnctor.jp", "9000")]


def test_sync_rep_incremental_without_deadline_never_consults_the_clock(monkeypatch) -> None:
    """cron・スクリプト等、期限を渡さない既存の呼び出し元の挙動は変わらない。"""
    fetched, saved = _incremental_fixture(monkeypatch, records=[("4001", ["m1"]), ("4002", ["m2"])])

    def fail_monotonic() -> float:
        raise AssertionError("monotonic() must not be called without a deadline")

    monkeypatch.setattr(sync.time, "monotonic", fail_monotonic)

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert fetched == ["m1", "m2"]
    assert count == 2
    assert saved == [("rep@cnctor.jp", "4001"), ("rep@cnctor.jp", "4002"), ("rep@cnctor.jp", "9000")]


def test_sync_rep_incremental_ignores_deadline_when_history_result_lacks_record_ids(monkeypatch) -> None:
    """レコードidが無い(古い形の)結果で途中終了しても、`historyId`を誤って進めない。"""
    from src.gmail_sync.gmail_client import HistoryListResult

    fetched, saved = _incremental_fixture(monkeypatch, records=[("x", ["m1", "m2"])])
    monkeypatch.setattr(
        sync.gmail_client,
        "list_history",
        lambda access_token, start_history_id: HistoryListResult(message_ids=["m1", "m2"], history_id="9000"),
    )
    _clock(monkeypatch, [0.0, 100.0])

    sync.sync_rep_incremental(
        "rep@cnctor.jp",
        "refresh-token",
        FakeContactClient({}),
        internal_domains=frozenset({"cnctor.jp"}),
        deadline=50.0,
    )

    assert fetched == ["m1"]
    assert saved == []


def test_sync_rep_incremental_checkpoints_past_a_record_whose_last_message_was_deleted(
    monkeypatch,
) -> None:
    """404でスキップしたメッセージ(既に削除済み)がレコードの最後でも、そのレコードは
    「処理し終えた」として再開位置に使う(kuma-qaレビューWARN対応: 打ち切り×404の組み合わせ)。"""
    fetched, saved = _incremental_fixture(
        monkeypatch, records=[("4001", ["m1", "m2"]), ("4002", ["m3", "m4"])]
    )

    def get_message(access_token, message_id):
        fetched.append(message_id)
        if message_id == "m2":
            raise GmailApiError(404, "gone")
        return _message(id_=message_id)

    monkeypatch.setattr(sync.gmail_client, "get_message", get_message)
    # m1=0, m2=1, m3=100(期限切れ)
    _clock(monkeypatch, [0.0, 1.0, 100.0])

    count = sync.sync_rep_incremental(
        "rep@cnctor.jp",
        "refresh-token",
        FakeContactClient({}),
        internal_domains=frozenset({"cnctor.jp"}),
        deadline=50.0,
    )

    assert fetched == ["m1", "m2"]
    assert count == 1
    assert saved == [("rep@cnctor.jp", "4001")]


def _full_scan_fixture(monkeypatch, message_ids: list[str]):
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        sync.gmail_client,
        "list_recent_messages",
        lambda access_token: [GmailMessageRef(id=m) for m in message_ids],
    )
    fetched: list[str] = []
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: fetched.append(message_id) or _message(id_=message_id),
    )
    monkeypatch.setattr(sync.db, "email_log_exists", lambda gmail_message_id: False)
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: None)
    monkeypatch.setattr(sync, "find_contact_page_id", lambda client, email: "contact-page-1")
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)
    return fetched


def test_sync_rep_stops_at_the_deadline(monkeypatch) -> None:
    """フルスキャンにも時間予算が効く(shirokuma-secレビューBLOCKER対応)。"""
    fetched = _full_scan_fixture(monkeypatch, ["m1", "m2", "m3"])
    _clock(monkeypatch, [0.0, 1.0, 100.0])

    count = sync.sync_rep(
        "rep@cnctor.jp",
        "refresh-token",
        FakeContactClient({}),
        internal_domains=frozenset({"cnctor.jp"}),
        deadline=50.0,
    )

    assert fetched == ["m1", "m2"]
    assert count == 2


def test_sync_rep_without_deadline_never_consults_the_clock(monkeypatch) -> None:
    fetched = _full_scan_fixture(monkeypatch, ["m1", "m2"])

    def fail_monotonic() -> float:
        raise AssertionError("monotonic() must not be called without a deadline")

    monkeypatch.setattr(sync.time, "monotonic", fail_monotonic)

    count = sync.sync_rep(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset({"cnctor.jp"})
    )

    assert fetched == ["m1", "m2"]
    assert count == 2


def test_sync_rep_incremental_passes_the_deadline_to_the_full_scan_fallbacks(monkeypatch) -> None:
    """`historyId`未設定・失効でフルスキャンへ退避するときも期限を引き継ぐ
    (shirokuma-secレビューBLOCKER対応: 復旧直後に一番踏みやすい経路)。"""
    seen: list[float | None] = []

    def fake_sync_rep(rep_email, refresh_token, contact_client, *, internal_domains, deadline=None):
        seen.append(deadline)
        return 0

    monkeypatch.setattr(sync, "sync_rep", fake_sync_rep)
    monkeypatch.setattr(sync.gmail_client, "refresh_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(sync.db, "update_history_id", lambda rep_email, history_id: None)

    # ① historyId未設定
    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection(None))
    sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset(), deadline=50.0
    )
    # ② historyId失効
    monkeypatch.setattr(sync.db, "find_connection_by_email", lambda rep_email: _stored_connection("old"))

    def raise_expired(access_token, start_history_id):
        raise HistoryIdExpiredError(404, "not found")

    monkeypatch.setattr(sync.gmail_client, "list_history", raise_expired)
    sync.sync_rep_incremental(
        "rep@cnctor.jp", "refresh-token", FakeContactClient({}), internal_domains=frozenset(), deadline=60.0
    )

    assert seen == [50.0, 60.0]
