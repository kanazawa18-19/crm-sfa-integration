"""既知のズレ以外のスキップだけをSlackへ上げる（2026-08-31）。

「89件あるから通知しない」は本末転倒（Geminiクロスレビュー指摘）。
既知は明示リストで切り離し、それ以外は通知する。誤報を鳴らし続けると
本物の通知まで無視されるようになるので、**分類を細かくする方向で解く。**
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import DispatchResult, PropertyDispatchResult
from src.sync_engine.known_sync_gaps import KNOWN_SYNC_GAPS
from src.sync_engine.production_wiring import SkipTrackingDispatcher
from src.sync_engine.sync_event import SyncEvent


class _FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def notify_update_skipped(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _FakeDispatcher:
    def __init__(self, result: DispatchResult) -> None:
        self._result = result

    def dispatch(self, event: SyncEvent) -> DispatchResult:
        return self._result


def _event(db_key: str) -> SyncEvent:
    return SyncEvent(
        source_tool=Tool.NOTION,
        db_key=db_key,
        external_id="page-1",
        properties={},
        occurred_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def _result(property_name: str, tool: Tool) -> DispatchResult:
    return DispatchResult(
        skipped=False,
        properties=(
            PropertyDispatchResult(
                property_name=property_name,
                resolution=None,
                written_tools=frozenset(),
                skipped_tools=frozenset({tool}),
            ),
        ),
    )


def test_known_gaps_do_not_notify() -> None:
    """既知の未対応項目は通知しない（89件が毎回鳴ると本物が埋もれる）。"""
    tool, db_key, property_name = next(iter(sorted(KNOWN_SYNC_GAPS, key=str)))
    notifier = _FakeNotifier()
    dispatcher = SkipTrackingDispatcher(
        _FakeDispatcher(_result(property_name, tool)), slack_notifier=notifier
    )

    dispatcher.dispatch(_event(db_key))

    assert notifier.calls == []


def test_unexpected_skips_notify() -> None:
    """既知リストに無いスキップは、本番障害の可能性があるので通知する。"""
    notifier = _FakeNotifier()
    dispatcher = SkipTrackingDispatcher(
        _FakeDispatcher(_result("案件名", Tool.ZOHO)), slack_notifier=notifier
    )

    dispatcher.dispatch(_event("project"))

    assert len(notifier.calls) == 1
    assert notifier.calls[0]["reason"] == "property_write_skipped"
    assert "案件名" in notifier.calls[0]["detail"]
    assert "zoho" in notifier.calls[0]["detail"]


def test_no_notifier_configured_is_not_an_error() -> None:
    dispatcher = SkipTrackingDispatcher(_FakeDispatcher(_result("案件名", Tool.ZOHO)))

    assert dispatcher.dispatch(_event("project")).has_partial_skips


def test_structurally_unwritable_properties_do_not_notify() -> None:
    """**外向きに構造的に書けない型は通知しない。**

    「営業ステータス」はSTATUS型でNotion→外部には絶対に書けないが、
    外部→Notionの変換表には載っているので既知のズレの表には入らない。
    案件のステージが動くたびにマネージャー全員へSlack DMが飛ぶところだった
    （obasan-qualityレビューBLOCKER、2026-08-31）。
    """
    notifier = _FakeNotifier()
    dispatcher = SkipTrackingDispatcher(
        _FakeDispatcher(_result("営業ステータス", Tool.KINTONE)), slack_notifier=notifier
    )

    dispatcher.dispatch(_event("project"))

    assert notifier.calls == []


def test_writable_property_that_fails_still_notifies() -> None:
    """送り先が決まっているのに書けなかった場合だけ通知する（本当に対処が要るもの）。"""
    notifier = _FakeNotifier()
    dispatcher = SkipTrackingDispatcher(
        _FakeDispatcher(_result("案件名", Tool.ZOHO)), slack_notifier=notifier
    )

    dispatcher.dispatch(_event("project"))

    assert len(notifier.calls) == 1
