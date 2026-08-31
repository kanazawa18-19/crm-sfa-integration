"""Notion→外部の同期ループ抑止（2026-08-31）。

無限ループ防止は本来 `X-Sync-System-ID` ヘッダーで行っているが、
**Notion の Webhook はカスタムヘッダーを送れない**ので効かない。
購読を作った瞬間に Zoho変更 → Notion更新 → Zoho更新 → … の反射が生まれる。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.sync_engine.webhook_handlers.notion_webhook import (
    handler_with_proxy,
    is_own_notion_write,
)

_BOT_ID = "3b4d8ea8-d4f3-81ee-b550-0027586fe38e"


def _page(editor_id: str) -> dict[str, Any]:
    return {
        "id": "26d6f1e2-0000-0000-0000-000000000000",
        "parent": {"type": "database_id", "database_id": "db-1"},
        "last_edited_time": "2026-08-31T09:00:00.000Z",
        "last_edited_by": {"object": "user", "id": editor_id},
        "properties": {},
    }


class _FakeClient:
    def __init__(self, page: dict[str, Any]) -> None:
        self._page = page
        self.fetches = 0

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        self.fetches += 1
        return self._page


def _event() -> dict[str, Any]:
    return {
        "headers": {},
        "body": json.dumps(
            {"entity": {"id": "26d6f1e2-0000-0000-0000-000000000000", "type": "page"}}
        ),
    }


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Notionは署名でしか認証しない。ここではループ抑止だけを見たいので、
    # 鍵を未設定にしてローカル開発用の抜け道を使う。
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")


def test_detects_our_own_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)

    assert is_own_notion_write(_page(_BOT_ID)) is True
    assert is_own_notion_write(_page("human-user-id")) is False


def test_cannot_decide_without_the_bot_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_SYNC_BOT_ID", raising=False)

    assert is_own_notion_write(_page(_BOT_ID)) is None


def test_our_own_write_is_not_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """自分が書いた結果の通知には、他ツールへ伝えるべき新しい情報が無い。

    外部発のイベントは1回のdispatchで全ツールへまとめて書き込まれるため、
    ここで捨ててもfan-outは失われない。
    """
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)
    client = _FakeClient(_page(_BOT_ID))
    dispatched: list[Any] = []

    class _Dispatcher:
        def dispatch(self, event: Any) -> Any:
            dispatched.append(event)
            raise AssertionError("自分の書き込みでdispatchしてはいけない")

    result = handler_with_proxy(
        _event(), None, notion_client=client, dispatcher=_Dispatcher()
    )

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"skipped": "own_system_write"}
    assert dispatched == []


def test_fails_closed_when_the_bot_id_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定材料が無いまま処理すると同期ループを起こす。書かない側へ倒す。"""
    monkeypatch.delenv("NOTION_SYNC_BOT_ID", raising=False)
    client = _FakeClient(_page("human-user-id"))

    class _Dispatcher:
        def dispatch(self, event: Any) -> Any:
            raise AssertionError("bot IDが無いままdispatchしてはいけない")

    result = handler_with_proxy(
        _event(), None, notion_client=client, dispatcher=_Dispatcher()
    )

    assert json.loads(result["body"]) == {"skipped": "sync_bot_id_not_configured"}


def test_the_page_is_fetched_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """ループ判定のために取得したページを、整形でも使い回すこと（二度取りしない）。"""
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)
    client = _FakeClient(_page("human-user-id"))

    handler_with_proxy(_event(), None, notion_client=client, dispatcher=None)

    assert client.fetches == 1


def test_pages_from_other_databases_are_skipped_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """購読はインテグレーション単位なので、同期対象外のDBの更新まで飛んでくる。

    IDマッピング用DB（「データマッピング」）はバックフィル中に数万件更新される。
    1件ごとにNotion APIでページを取りに行くのは純粋な無駄なので、
    **取りに行く前に**親DBを見て弾く。
    """
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)
    client = _FakeClient(_page("human-user-id"))
    event = {
        "headers": {},
        "body": json.dumps(
            {
                "entity": {"id": "3bad8ea8-d4f3-8131-85c8-da41833aef2d", "type": "page"},
                "data": {"parent": {"id": "3b9d8ea8-d4f3-8059-8b04-ee5308d2cbf0"}},
            }
        ),
    }

    result = handler_with_proxy(event, None, notion_client=client, dispatcher=None)

    assert json.loads(result["body"]) == {"skipped": "not_a_synced_database"}
    assert client.fetches == 0


def test_pages_from_synced_databases_are_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    """同期対象のDBなら、ハイフンの有無に関わらず通すこと。"""
    from src.db_schema.registry import ALL_SCHEMAS

    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)
    database_id = next(s.notion_database_id for s in ALL_SCHEMAS if s.key == "action")
    client = _FakeClient(_page("human-user-id"))
    event = {
        "headers": {},
        "body": json.dumps(
            {
                "entity": {"id": "26d6f1e2-0000-0000-0000-000000000000", "type": "page"},
                "data": {"parent": {"id": database_id.replace("-", "")}},
            }
        ),
    }

    handler_with_proxy(event, None, notion_client=client, dispatcher=None)

    assert client.fetches == 1


def test_event_author_wins_over_the_page_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    """**人が編集した直後に同期エンジンが同じページへ書くと、取得時点の
    最終更新者はbotになっている。** ページの最終更新者で判定すると、
    その人の編集を丸ごと捨ててしまう（ChatGPTクロスレビューBLOCKER、2026-08-31）。

    Webhookの`authors`は「そのイベントを起こした人」なので、こちらを優先する。
    """
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)

    # ページの最終更新者はbot（人の編集の後に同期エンジンが書いた）。
    page = _page(_BOT_ID)

    # イベントの作者は人。
    assert is_own_notion_write(page, {"authors": [{"id": "human-user-id"}]}) is False
    # イベントの作者がbotなら、これは自分の書き込み。
    assert is_own_notion_write(page, {"authors": [{"id": _BOT_ID}]}) is True


def test_mixed_authors_are_not_treated_as_our_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """イベントがまとめられて人とbotの両方が作者になることがある。

    1人でも人が入っていれば、伝えるべき変更が含まれている可能性がある。捨てない。
    """
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)

    result = is_own_notion_write(
        _page(_BOT_ID), {"authors": [{"id": _BOT_ID}, {"id": "human-user-id"}]}
    )

    assert result is False


def test_falls_back_to_the_page_editor_when_authors_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`authors`が無い形式のペイロードでは、従来どおり最終更新者で判定する。"""
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)

    assert is_own_notion_write(_page(_BOT_ID), {}) is True
    assert is_own_notion_write(_page("human-user-id"), {}) is False
