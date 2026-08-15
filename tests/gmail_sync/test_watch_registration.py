from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.gmail_sync import db, watch_registration
from src.gmail_sync.watch_registration import (
    GmailWatchNotConfiguredError,
    register_or_renew_watch,
    renew_all_watches,
)


def _connection(
    rep_email: str = "rep@cnctor.jp",
    *,
    watch_expiration: datetime | None = None,
) -> db.RepGmailConnection:
    return db.RepGmailConnection(
        rep_email=rep_email,
        refresh_token_enc="encrypted-refresh-token",
        last_synced_at=None,
        history_id="1000",
        watch_expiration=watch_expiration,
    )


def test_register_or_renew_watch_saves_history_id_and_expiration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_registration.gmail_client, "refresh_access_token", lambda refresh_token: "access-token"
    )
    monkeypatch.setattr(
        watch_registration.gmail_client,
        "watch_mailbox",
        lambda access_token, topic_name: {"historyId": "5000", "expiration": "1755600000000"},
    )

    saved: list[tuple] = []
    monkeypatch.setattr(
        watch_registration.db,
        "update_watch_state",
        lambda rep_email, history_id, expiration: saved.append((rep_email, history_id, expiration)),
    )

    register_or_renew_watch("rep@cnctor.jp", "refresh-token", "projects/test/topics/gmail-notifications")

    assert len(saved) == 1
    rep_email, history_id, expiration = saved[0]
    assert rep_email == "rep@cnctor.jp"
    assert history_id == "5000"
    assert expiration == datetime.fromtimestamp(1755600000000 / 1000, tz=timezone.utc)


def test_register_or_renew_watch_raises_when_response_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_registration.gmail_client, "refresh_access_token", lambda refresh_token: "access-token"
    )
    monkeypatch.setattr(
        watch_registration.gmail_client, "watch_mailbox", lambda access_token, topic_name: {}
    )

    with pytest.raises(watch_registration.gmail_client.GmailApiError):
        register_or_renew_watch("rep@cnctor.jp", "refresh-token", "projects/test/topics/gmail-notifications")


def test_renew_all_watches_raises_when_topic_name_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GMAIL_PUBSUB_TOPIC_NAME", raising=False)

    with pytest.raises(GmailWatchNotConfiguredError):
        renew_all_watches()


def test_renew_all_watches_skips_reps_with_far_future_expiration(monkeypatch: pytest.MonkeyPatch) -> None:
    far_future = datetime.now(timezone.utc) + timedelta(days=5)
    monkeypatch.setattr(
        watch_registration.db, "list_gmail_connections", lambda: [_connection(watch_expiration=far_future)]
    )
    called: list[str] = []
    monkeypatch.setattr(
        watch_registration,
        "register_or_renew_watch",
        lambda rep_email, refresh_token, topic_name: called.append(rep_email),
    )
    monkeypatch.setattr(watch_registration, "decrypt_token", lambda enc: "refresh-token")

    result = renew_all_watches(topic_name="projects/test/topics/gmail-notifications")

    assert result == {"rep@cnctor.jp": "skipped"}
    assert called == []


def test_renew_all_watches_renews_reps_with_expiring_or_missing_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    monkeypatch.setattr(
        watch_registration.db,
        "list_gmail_connections",
        lambda: [
            _connection("expiring@cnctor.jp", watch_expiration=soon),
            _connection("never-registered@cnctor.jp", watch_expiration=None),
        ],
    )
    called: list[str] = []
    monkeypatch.setattr(
        watch_registration,
        "register_or_renew_watch",
        lambda rep_email, refresh_token, topic_name: called.append(rep_email),
    )
    monkeypatch.setattr(watch_registration, "decrypt_token", lambda enc: "refresh-token")

    result = renew_all_watches(topic_name="projects/test/topics/gmail-notifications")

    assert result == {"expiring@cnctor.jp": "renewed", "never-registered@cnctor.jp": "renewed"}
    assert set(called) == {"expiring@cnctor.jp", "never-registered@cnctor.jp"}


def test_renew_all_watches_isolates_failures_per_rep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_registration.db,
        "list_gmail_connections",
        lambda: [_connection("fails@cnctor.jp", watch_expiration=None), _connection("ok@cnctor.jp", watch_expiration=None)],
    )
    monkeypatch.setattr(watch_registration, "decrypt_token", lambda enc: "refresh-token")

    def fake_register(rep_email: str, refresh_token: str, topic_name: str) -> None:
        if rep_email == "fails@cnctor.jp":
            raise RuntimeError("boom")

    monkeypatch.setattr(watch_registration, "register_or_renew_watch", fake_register)

    result = renew_all_watches(topic_name="projects/test/topics/gmail-notifications")

    assert result["ok@cnctor.jp"] == "renewed"
    assert result["fails@cnctor.jp"].startswith("error:")
