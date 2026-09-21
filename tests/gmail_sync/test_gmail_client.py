from __future__ import annotations

import pytest

from src.gmail_sync.gmail_client import (
    GmailApiError,
    HistoryIdExpiredError,
    list_history,
    list_messages_page,
    watch_mailbox,
)

_ACCESS_TOKEN = "access-token"
_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


def test_watch_mailbox_returns_history_id_and_expiration(requests_mock) -> None:
    requests_mock.post(
        f"{_BASE_URL}/watch",
        json={"historyId": "12345", "expiration": "1755600000000"},
    )

    result = watch_mailbox(_ACCESS_TOKEN, "projects/test/topics/gmail-notifications")

    assert result == {"historyId": "12345", "expiration": "1755600000000"}
    request = requests_mock.request_history[0]
    assert request.json() == {
        "topicName": "projects/test/topics/gmail-notifications",
        "labelIds": ["INBOX"],
    }


def test_watch_mailbox_raises_on_error(requests_mock) -> None:
    requests_mock.post(f"{_BASE_URL}/watch", status_code=403, json={"error": "forbidden"})

    with pytest.raises(GmailApiError):
        watch_mailbox(_ACCESS_TOKEN, "projects/test/topics/gmail-notifications")


def test_list_history_returns_added_message_ids_and_history_id(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        json={
            "history": [
                {"messagesAdded": [{"message": {"id": "msg1"}}]},
                {"messagesAdded": [{"message": {"id": "msg2"}}, {"message": {"id": "msg3"}}]},
            ],
            "historyId": "5000",
        },
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1", "msg2", "msg3"]
    assert result.history_id == "5000"


def test_list_history_follows_pagination_and_uses_last_page_history_id(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        [
            {
                "json": {
                    "history": [{"id": "3001", "messagesAdded": [{"message": {"id": "msg1"}}]}],
                    "nextPageToken": "page2",
                    "historyId": "4000",
                }
            },
            {
                "json": {
                    "history": [{"id": "3002", "messagesAdded": [{"message": {"id": "msg2"}}]}],
                    "historyId": "5000",
                }
            },
        ],
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1", "msg2"]
    # ページを跨いでもレコードidの対応が保たれる(再開位置に使う)。
    assert result.message_history_ids == {"msg1": "3001", "msg2": "3002"}
    # 最後のページの値を採用する(shirokuma-secレビューWARN対応: レスポンス自体に含まれる
    # historyIdを使うことで、list_history()完了後に別途get_profile()を呼ぶ場合に生じる
    # レース(その間の新着メールのhistoryIdを見逃す)を避ける)。
    assert result.history_id == "5000"
    assert requests_mock.request_history[1].qs["pagetoken"] == ["page2"]


def test_list_history_returns_none_history_id_when_absent_from_response(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        json={"history": [{"messagesAdded": [{"message": {"id": "msg1"}}]}]},
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1"]
    assert result.history_id is None


def test_list_history_raises_history_id_expired_on_404(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/history", status_code=404, json={"error": "not found"})

    with pytest.raises(HistoryIdExpiredError):
        list_history(_ACCESS_TOKEN, "too-old")


# --- list_messages_page（過去分の取り込み用、2026-09-03） -------------------------------------


def test_list_messages_page_returns_ids_and_next_page_token(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/messages",
        json={"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "tok-2"},
    )

    page = list_messages_page(_ACCESS_TOKEN, query="newer_than:365d")

    assert [ref.id for ref in page.refs] == ["m1", "m2"]
    assert page.next_page_token == "tok-2"
    request = requests_mock.request_history[0]
    assert request.qs["q"] == ["newer_than:365d"]
    assert "pagetoken" not in request.qs


def test_list_messages_page_sends_page_token_when_given(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/messages", json={"messages": [], "nextPageToken": None})

    page = list_messages_page(_ACCESS_TOKEN, query="q", page_token="tok-2")

    assert page.refs == []
    assert page.next_page_token is None
    assert requests_mock.request_history[0].qs["pagetoken"] == ["tok-2"]


def test_list_messages_page_returns_empty_on_no_results(requests_mock) -> None:
    """Gmailはヒット0件のとき`messages`キー自体を返さない。"""
    requests_mock.get(f"{_BASE_URL}/messages", json={})

    page = list_messages_page(_ACCESS_TOKEN, query="q")

    assert page.refs == []
    assert page.next_page_token is None


def test_list_messages_page_caps_max_results_at_the_api_limit(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/messages", json={"messages": []})

    list_messages_page(_ACCESS_TOKEN, query="q", max_results=100000)

    assert requests_mock.request_history[0].qs["maxresults"] == ["500"]


def test_list_messages_page_raises_on_error(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/messages", status_code=403, json={"error": "forbidden"})

    with pytest.raises(GmailApiError):
        list_messages_page(_ACCESS_TOKEN, query="q")


def test_list_history_records_the_history_record_id_of_each_message(requests_mock) -> None:
    """時間予算で途中終了したときの再開位置に使う(2026-09-21)。"""
    requests_mock.get(
        f"{_BASE_URL}/history",
        json={
            "history": [
                {"id": "4001", "messagesAdded": [{"message": {"id": "msg1"}}]},
                {"id": 4002, "messagesAdded": [{"message": {"id": "msg2"}}, {"message": {"id": "msg3"}}]},
                {"messagesAdded": [{"message": {"id": "msg4"}}]},
            ],
            "historyId": "5000",
        },
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1", "msg2", "msg3", "msg4"]
    # idは文字列に揃える。idの無いレコード(想定外)は再開位置に使わない。
    assert result.message_history_ids == {"msg1": "4001", "msg2": "4002", "msg3": "4002"}


def test_rate_limit_retries_default_to_the_interactive_value_and_can_be_widened(monkeypatch) -> None:
    """Gmail API呼び出しの429リトライ回数は既定でリクエスト/レスポンス型向けの小さい値
    (Vercel関数の300秒に収まる側が既定)。`bounded_rate_limit_retries()`の中でだけ変わり、
    抜けたら戻る(2026-09-21)。"""
    from src.gmail_sync import gmail_client
    from src.sync_engine.clients._http import (
        DEFAULT_MAX_RATE_LIMIT_RETRIES,
        INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
    )

    seen: list[int] = []

    class FakeResponse:
        status_code = 200
        ok = True
        headers: dict = {}

        @staticmethod
        def json():
            return {"id": "msg1", "payload": {"headers": []}, "snippet": ""}

    def fake_request_with_retry(method, url, **kwargs):
        seen.append(kwargs["max_rate_limit_retries"])
        return FakeResponse()

    monkeypatch.setattr(gmail_client, "request_with_retry", fake_request_with_retry)

    gmail_client.get_message(_ACCESS_TOKEN, "msg1")
    with gmail_client.bounded_rate_limit_retries(DEFAULT_MAX_RATE_LIMIT_RETRIES):
        gmail_client.get_message(_ACCESS_TOKEN, "msg1")
        gmail_client.list_history(_ACCESS_TOKEN, "1000")
    gmail_client.get_message(_ACCESS_TOKEN, "msg1")

    assert seen == [
        INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        DEFAULT_MAX_RATE_LIMIT_RETRIES,
        DEFAULT_MAX_RATE_LIMIT_RETRIES,
        INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
    ]
    assert gmail_client.current_max_rate_limit_retries() == INTERACTIVE_MAX_RATE_LIMIT_RETRIES
    assert INTERACTIVE_MAX_RATE_LIMIT_RETRIES < DEFAULT_MAX_RATE_LIMIT_RETRIES


def test_every_gmail_request_passes_the_current_rate_limit_retries(monkeypatch) -> None:
    """呼び出しを1つでも取りこぼすと、その1つだけ既定の30回に戻って関数ごとタイムアウトする。"""
    from src.gmail_sync import gmail_client

    seen: list[int] = []

    class FakeResponse:
        status_code = 200
        ok = True
        headers: dict = {}

        @staticmethod
        def json():
            return {
                "access_token": "tok",
                "messages": [],
                "history": [],
                "id": "msg1",
                "payload": {"headers": []},
                "snippet": "",
            }

    def fake_request_with_retry(method, url, **kwargs):
        seen.append(kwargs["max_rate_limit_retries"])
        return FakeResponse()

    monkeypatch.setattr(gmail_client, "request_with_retry", fake_request_with_retry)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csec")

    with gmail_client.bounded_rate_limit_retries(2):
        gmail_client.refresh_access_token("refresh")
        gmail_client.list_recent_messages(_ACCESS_TOKEN)
        gmail_client.list_messages_page(_ACCESS_TOKEN, query="q")
        gmail_client.watch_mailbox(_ACCESS_TOKEN, "projects/p/topics/t")
        gmail_client.list_history(_ACCESS_TOKEN, "1000")
        gmail_client.get_message(_ACCESS_TOKEN, "msg1")

    assert seen == [2] * 6


def test_bounded_rate_limit_retries_does_not_leak_between_concurrent_threadpool_requests() -> None:
    """FastAPIのルートが`run_in_threadpool`で同時に2本動かしても、片方の絞りがもう片方や
    既定値へ漏れない(kuma-qaレビューWARN対応: 並行時の非汚染を自動テストに残す)。
    anyioは`run_sync`でcontextvarsをスレッドへ複製するので、各リクエストが自分の値だけを見る。"""
    import threading

    import anyio
    from starlette.concurrency import run_in_threadpool

    from src.gmail_sync import gmail_client
    from src.sync_engine.clients._http import INTERACTIVE_MAX_RATE_LIMIT_RETRIES

    both_started = threading.Barrier(2, timeout=5)
    seen: dict[str, list[int]] = {"a": [], "b": []}

    def request(name: str, bound: int) -> None:
        with gmail_client.bounded_rate_limit_retries(bound):
            seen[name].append(gmail_client.current_max_rate_limit_retries())
            both_started.wait()  # 相手も絞りの中に入るまで待ってから、もう一度自分の値を見る
            seen[name].append(gmail_client.current_max_rate_limit_retries())

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_in_threadpool, request, "a", 2)
            tg.start_soon(run_in_threadpool, request, "b", 5)

    anyio.run(main)

    assert seen == {"a": [2, 2], "b": [5, 5]}
    assert gmail_client.current_max_rate_limit_retries() == INTERACTIVE_MAX_RATE_LIMIT_RETRIES
