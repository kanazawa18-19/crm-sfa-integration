"""WebhookSlackNotifier（05_同期・競合制御「アラート通知」）の単体テスト。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import RejectedData
from src.sync_engine.slack_notifier import WebhookSlackNotifier

REJECTED = RejectedData(
    record_id="MSA-PJ-001",
    property_name="営業ステータス",
    adopted_value="商談中(B)",
    adopted_tool=Tool.NOTION,
    rejected_value="失注",
    rejected_tool=Tool.KINTONE,
    occurred_at=datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc),
)


def test_notify_conflict_posts_to_configured_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> None:
        calls.append({"url": url, "json": json, "timeout": timeout})

    monkeypatch.setattr("src.sync_engine.slack_notifier.requests.post", fake_post)
    notifier = WebhookSlackNotifier("https://hooks.slack.com/services/xxx")

    notifier.notify_conflict(REJECTED)

    assert len(calls) == 1
    assert calls[0]["url"] == "https://hooks.slack.com/services/xxx"
    text = calls[0]["json"]["text"]
    assert "MSA-PJ-001" in text
    assert "営業ステータス" in text
    assert "商談中(B)" in text
    assert "失注" in text
    assert "採用元: notion" in text
    assert "却下元: kintone" in text


def test_notify_conflict_uses_env_var_when_url_not_explicitly_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "src.sync_engine.slack_notifier.requests.post",
        lambda url, json, timeout: calls.append(url),
    )
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/from-env")
    notifier = WebhookSlackNotifier()

    notifier.notify_conflict(REJECTED)

    assert calls == ["https://hooks.slack.com/services/from-env"]


def test_notify_conflict_skips_when_no_webhook_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "src.sync_engine.slack_notifier.requests.post",
        lambda url, json, timeout: calls.append(url),
    )
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT", raising=False)
    notifier = WebhookSlackNotifier()

    notifier.notify_conflict(REJECTED)

    assert calls == []


# --- notify_new_record_created / notify_new_record_issue（2026-08-25、Round2） ------------------
# shirokuma-sec/obasan-qualityレビューWARN対応: 新規レコード作成の運用可視性のために追加。
# 本番環境にSLACK_WEBHOOK_URL_ALERTが未設定と判明したため、incident_detectionと同じ
# 「User.isManager = true」全員へのSlack DM方式へ変更した（2026-08-25）。
# ここでは`manager_dm.notify_managers()`が正しい引数で呼ばれることのみ検証し、DM解決・
# 送信自体の詳細な挙動（SLACK_BOT_TOKEN未設定時のスキップ、manager毎のtry/except等）は
# tests/notifications/test_manager_dm.pyの責務とする。


def test_notify_new_record_created_calls_manager_dm_notify_managers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text, "log_context": log_context}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_created(
        db_key="client_master",
        source_tool=Tool.KINTONE,
        external_id="45",
        notion_page_id="new-page-id",
    )

    assert len(calls) == 1
    text = calls[0]["text"]
    assert "client_master" in text
    assert "kintone" in text
    assert "45" in text
    assert "new-page-id" in text
    assert calls[0]["log_context"] == "WebhookSlackNotifier"


def test_notify_new_record_issue_calls_manager_dm_notify_managers_with_notion_page_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """孤児ページ（IdMapping登録失敗）の場合、Notion page IDがテキストに明示的に含まれること
    （運用者がすぐに該当ページを特定できるようにするため）。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text, "log_context": log_context}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_issue(
        db_key="project",
        source_tool=Tool.ZOHO,
        external_id="zoho-1",
        reason="mapping_registration_failed",
        detail="Notionページ作成後、IdMapping登録に失敗しました。error=RuntimeError('x')",
        notion_page_id="orphaned-page-id",
    )

    assert len(calls) == 1
    text = calls[0]["text"]
    assert "project" in text
    assert "zoho" in text
    assert "mapping_registration_failed" in text
    assert "orphaned-page-id" in text


def test_notify_new_record_issue_omits_notion_page_id_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_issue(
        db_key="action",
        source_tool=Tool.KINTONE,
        external_id="77",
        reason="missing_required_properties",
        detail="必須プロパティが不足しているため作成をスキップしました",
    )

    assert "Notion page ID" not in calls[0]["text"]


# --- DM本文への人間向け表示名・対処アクション埋め込み（obasan-qualityレビュー対応、2026-08-25） --
# 内部識別子（db_key/reason）の生値だけでなく、緊急時にその場で判断できる情報をDM本文に含める。


def test_notify_new_record_created_includes_db_key_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_created(
        db_key="client_master", source_tool=Tool.KINTONE, external_id="45", notion_page_id="new-page-id"
    )

    text = calls[0]["text"]
    assert "client_master" in text
    assert "取引先マスターDB" in text


def test_notify_new_record_issue_includes_action_hint_for_mapping_registration_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_issue(
        db_key="project",
        source_tool=Tool.ZOHO,
        external_id="zoho-1",
        reason="mapping_registration_failed",
        detail="detail",
        notion_page_id="orphaned-page-id",
    )

    text = calls[0]["text"]
    assert "案件管理DB" in text  # db_keyの人間向け表示名
    assert "Notion page ID" in text and "アーカイブ済みか確認" in text  # reasonの対処アクション


def test_notify_new_record_issue_includes_action_hint_for_notion_creation_status_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_issue(
        db_key="client_master",
        source_tool=Tool.KINTONE,
        external_id="45",
        reason="notion_creation_status_unknown",
        detail="detail",
    )

    assert "監査ログとNotion上を突き合わせて" in calls[0]["text"]


def test_notify_new_record_issue_includes_action_hint_for_source_record_fetch_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-08-27本番障害対応で追加したreason="source_record_fetch_failed"の対処アクション
    行が含まれること（kuma-qaレビューINFO対応、他のreasonと同じパターンでテストを揃える）。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_issue(
        db_key="client_master",
        source_tool=Tool.KINTONE,
        external_id="kintone-broken",
        reason="source_record_fetch_failed",
        detail="detail",
    )

    assert "kintone/Zoho側APIの障害・レート制限" in calls[0]["text"]


def test_notify_new_record_issue_omits_action_hint_for_unknown_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_ISSUE_REASON_ACTION_HINTS`に無いreasonでも例外を送出せず、単に対応行を省くこと。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_issue(
        db_key="client_master",
        source_tool=Tool.KINTONE,
        external_id="45",
        reason="some_future_reason",
        detail="detail",
    )

    assert "対応:" not in calls[0]["text"]


def test_notify_new_record_created_falls_back_to_raw_db_key_when_schema_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知のdb_keyでも`get_schema()`のKeyErrorを握りつぶし、DM本文の生成自体は失敗させないこと。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.notifications.manager_dm.notify_managers",
        lambda text, *, log_context: calls.append({"text": text}),
    )
    notifier = WebhookSlackNotifier()

    notifier.notify_new_record_created(
        db_key="unknown_db_key", source_tool=Tool.KINTONE, external_id="1", notion_page_id="p"
    )

    assert "unknown_db_key" in calls[0]["text"]


# --- 例外を投げない設計（2026-08-25、3回目最終レビューBLOCKER対応） -----------------------------
# `requests.post()`が例外を送出しても、呼び出し元（Dispatcherの保護ロジック自体を含む）を
# 巻き込んで失敗させないよう、各notify_*メソッドは例外を握りつぶしログのみ残すこと。


def test_notify_conflict_does_not_raise_when_requests_post_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(url: str, json: dict[str, Any], timeout: int) -> None:
        raise TimeoutError("slack webhook timed out")

    monkeypatch.setattr("src.sync_engine.slack_notifier.requests.post", _raise)
    notifier = WebhookSlackNotifier("https://hooks.slack.com/services/xxx")

    with caplog.at_level("WARNING"):
        notifier.notify_conflict(REJECTED)  # 例外を送出しないこと自体がこのテストの主眼。

    assert any("failed to post to Slack" in r.getMessage() for r in caplog.records)


def test_notify_new_record_created_does_not_raise_when_manager_dm_notify_managers_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DM送信方式への変更後も、`manager_dm.notify_managers()`が万一例外を送出した場合に
    備えて`_notify_managers()`自体がそれを握りつぶすこと（`manager_dm.notify_managers()`は
    本来例外を投げない設計だが、`_post()`と同じ多層防御としてここでも確認する）。"""

    def _raise(text: str, *, log_context: str) -> None:
        raise ConnectionError("connection reset")

    monkeypatch.setattr("src.notifications.manager_dm.notify_managers", _raise)
    notifier = WebhookSlackNotifier()

    with caplog.at_level("WARNING"):
        notifier.notify_new_record_created(
            db_key="client_master", source_tool=Tool.KINTONE, external_id="45", notion_page_id="x"
        )

    assert any("failed to notify managers via Slack DM" in r.getMessage() for r in caplog.records)


def test_notify_new_record_issue_does_not_raise_when_manager_dm_notify_managers_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_handle_uncertain_notion_page_creation()`/`_handle_orphaned_notion_page()`のような
    「他の保護ロジックが失敗した後の最終防衛線」で使われる通知のため、特に重要。"""

    def _raise(text: str, *, log_context: str) -> None:
        raise RuntimeError("HTTP 500 from Slack")

    monkeypatch.setattr("src.notifications.manager_dm.notify_managers", _raise)
    notifier = WebhookSlackNotifier()

    with caplog.at_level("WARNING"):
        notifier.notify_new_record_issue(
            db_key="project",
            source_tool=Tool.ZOHO,
            external_id="zoho-1",
            reason="mapping_registration_failed",
            detail="detail",
            notion_page_id="orphaned-page-id",
        )

    assert any("failed to notify managers via Slack DM" in r.getMessage() for r in caplog.records)
