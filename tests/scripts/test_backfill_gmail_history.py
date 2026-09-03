"""Gmail過去分取り込みスクリプトのうち、外部に触らない部分の検証（2026-09-03）。

Gmail API・Notion API・DBを叩く部分は、認証情報が要るためここでは検証しない
（実地確認は`scripts/verify_gmail_backfill.py`で行う）。
"""

from __future__ import annotations

from scripts.backfill_gmail_history import (
    _build_query,
    _chunks,
    build_contact_index,
    load_or_build_contact_index,
)


def test_chunks_splits_without_dropping_or_duplicating() -> None:
    items = [str(i) for i in range(37)]

    batches = list(_chunks(items, 15))

    assert [len(b) for b in batches] == [15, 15, 7]
    assert [x for b in batches for x in b] == items


def test_chunks_of_empty_sequence_yields_nothing() -> None:
    assert list(_chunks([], 15)) == []


def test_build_query_ors_from_and_to_for_each_address() -> None:
    query = _build_query(["a@example.com", "b@example.com"], days=365, before=date(2026, 8, 25))

    assert query.startswith("{")
    assert "from:a@example.com to:a@example.com" in query
    assert "from:b@example.com to:b@example.com" in query


def test_build_query_does_not_search_cc() -> None:
    """突合側(classify_message)はFrom/Toしか見ない。検索だけccを含めても取得が無駄になる。"""
    query = _build_query(["a@example.com"], days=365, before=date(2026, 8, 25))

    assert "cc:" not in query


def test_build_query_stops_before_the_live_sync_window() -> None:
    """★ 通常同期が面倒を見ている期間と重ならない。重なると通常同期の通知が黙って止まる。"""
    query = _build_query(["a@example.com"], days=365, before=date(2026, 8, 25))

    assert "before:2026/8/25" in query


def test_build_query_limits_window_and_excludes_spam_and_trash() -> None:
    query = _build_query(["a@example.com"], days=90, before=date(2026, 8, 25))

    assert "newer_than:90d" in query
    assert "-in:spam" in query
    assert "-in:trash" in query


def test_build_index_skips_pages_without_a_usable_email(monkeypatch) -> None:
    """メールアドレスが空・型違いのページは索引に入れない。"""

    class _FakeClient:
        def query_all_pages(self, *, filter=None, page_size=100):
            return [
                {
                    "id": "cnt-1",
                    "properties": {"メールアドレス": {"type": "email", "email": "A@Example.com"}},
                },
                {"id": "cnt-2", "properties": {"メールアドレス": {"type": "email", "email": None}}},
                {"id": "cnt-3", "properties": {}},
                # 先勝ち（同じアドレスが2件あってもcnt-1を保つ）
                {
                    "id": "cnt-4",
                    "properties": {"メールアドレス": {"type": "email", "email": "a@example.com"}},
                },
            ]

    import scripts.backfill_gmail_history as mod

    monkeypatch.setattr(mod, "HttpNotionClient", lambda *a, **k: _FakeClient())

    index = build_contact_index()

    assert index == {"a@example.com": "cnt-1"}


def test_load_or_build_contact_index_writes_then_reads_the_cache(tmp_path, monkeypatch) -> None:
    """索引の作成はNotionを3万件読んで約6分かかる。流し直しで毎回待たないためのキャッシュ。"""
    import scripts.backfill_gmail_history as mod

    cache = tmp_path / "index.json"
    builds = {"count": 0}

    def fake_build() -> dict[str, str]:
        builds["count"] += 1
        return {"a@example.com": "cnt-1"}

    monkeypatch.setattr(mod, "build_contact_index", fake_build)

    first = load_or_build_contact_index(str(cache))
    second = load_or_build_contact_index(str(cache))

    assert first == second == {"a@example.com": "cnt-1"}
    assert builds["count"] == 1, "2回目はキャッシュから読むので作り直さない"
    assert cache.exists()


def test_load_or_build_contact_index_without_cache_path_always_rebuilds(monkeypatch) -> None:
    import scripts.backfill_gmail_history as mod

    builds = {"count": 0}

    def fake_build() -> dict[str, str]:
        builds["count"] += 1
        return {}

    monkeypatch.setattr(mod, "build_contact_index", fake_build)

    load_or_build_contact_index(None)
    load_or_build_contact_index(None)

    assert builds["count"] == 2


# --- backfill_rep / main（外部I/Oはフェイクに差し替える、2026-09-03） ------------------------
#
# ここが本体。過去1年分・全担当を対象に一括で書き込むスクリプトなので、
# 「--apply を付けない限り1通も書き込まない」をコードで担保しておく。

from dataclasses import dataclass  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

import scripts.backfill_gmail_history as mod  # noqa: E402
from src.gmail_sync.gmail_client import (  # noqa: E402
    GmailApiError,
    GmailMessagePage,
    GmailMessageRef,
)
from src.gmail_sync.db import EmailLogRow  # noqa: E402


@dataclass
class _FakeMessage:
    id: str
    from_header: str
    to_header: str
    subject: str | None = "件名"
    date_header: str | None = "Tue, 01 Sep 2026 10:00:00 +0900"
    snippet: str | None = "本文の先頭"
    thread_id: str | None = "t1"
    internal_date_ms: str | None = "1788310800000"  # 2026-09-01 10:00 JST


class _FakeGmail:
    """`gmail_client`の差し替え。検索結果とメッセージ本体をテストから与える。"""

    GmailApiError = GmailApiError

    def __init__(
        self,
        *,
        search_results: dict[str, list[str]] | None = None,
        messages: dict[str, _FakeMessage] | None = None,
        get_errors: dict[str, GmailApiError] | None = None,
        search_error_on: set[str] | None = None,
    ) -> None:
        self.search_results = search_results or {}
        self.messages = messages or {}
        self.get_errors = get_errors or {}
        self.search_error_on = search_error_on or set()
        self.get_calls: list[str] = []

    def refresh_access_token(self, refresh_token: str) -> str:
        return "access-token"

    def list_messages_page(self, access_token, *, query, page_token=None, max_results=500):
        for marker in self.search_error_on:
            if marker in query:
                raise GmailApiError(500, "boom")
        ids = [m for addr, msgs in self.search_results.items() if addr in query for m in msgs]
        return GmailMessagePage(
            refs=[GmailMessageRef(id=i) for i in dict.fromkeys(ids)],
            next_page_token=None,
        )

    def get_message(self, access_token: str, message_id: str) -> _FakeMessage:
        self.get_calls.append(message_id)
        if message_id in self.get_errors:
            raise self.get_errors[message_id]
        return self.messages[message_id]


class _FakeDb:
    def __init__(self) -> None:
        self.inserted: list = []
        self.EmailLogRow = EmailLogRow

    def insert_email_logs(self, rows) -> int:
        self.inserted.extend(rows)
        return len(rows)


@pytest.fixture
def gmail_env(monkeypatch):
    """社内ドメインの環境変数だけ固定する（未設定だと環境依存になる）。"""
    monkeypatch.setenv("INTERNAL_EMAIL_DOMAINS", "cnctor.jp")


def _run(fake_gmail: _FakeGmail, fake_db: _FakeDb, monkeypatch, **kwargs):
    monkeypatch.setattr(mod, "gmail_client", fake_gmail)
    monkeypatch.setattr(mod, "db", fake_db)
    params = {
        "rep_email": "rep@cnctor.jp",
        "refresh_token": "rt",
        "contact_index": {"lead@client.example.com": "cnt-1"},
        "existing_ids": set(),
        "days": 365,
        "before": date(2026, 8, 25),
        "apply": False,
    }
    params.update(kwargs)
    return mod.backfill_rep(**params)


def _inbound_message(msg_id: str = "m1") -> _FakeMessage:
    return _FakeMessage(
        id=msg_id,
        from_header="Lead <lead@client.example.com>",
        to_header="Rep <rep@cnctor.jp>",
    )


def _outbound_message(msg_id: str = "m2") -> _FakeMessage:
    return _FakeMessage(
        id=msg_id,
        from_header="Rep <rep@cnctor.jp>",
        to_header="Lead <lead@client.example.com>",
    )


def test_backfill_rep_writes_nothing_without_apply(gmail_env, monkeypatch) -> None:
    """★ 試算モードでは1通も書き込まない。運用手順「まず試算」の土台。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1"]},
        messages={"m1": _inbound_message()},
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=False)

    assert db.inserted == []
    assert result.inserted == 0
    # 「何件入るはずか」は試算でも分かる。
    assert result.matched == 1
    assert result.inbound == 1


def test_backfill_rep_inserts_when_applied(gmail_env, monkeypatch) -> None:
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1", "m2"]},
        messages={"m1": _inbound_message(), "m2": _outbound_message()},
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.inserted == 2
    assert result.inbound == 1
    assert result.outbound == 1
    assert {r.gmail_message_id for r in db.inserted} == {"m1", "m2"}
    assert db.inserted[0].contact_page_id == "cnt-1"
    assert db.inserted[0].rep_email == "rep@cnctor.jp"


def test_backfill_rep_skips_messages_already_recorded(gmail_env, monkeypatch) -> None:
    """記録済みのメールはヘッダ取得すらしない（API枠と時間の無駄）。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1", "m2"]},
        messages={"m2": _outbound_message()},
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, existing_ids={"m1"}, apply=True)

    assert result.already_recorded == 1
    assert gmail.get_calls == ["m2"]
    assert result.inserted == 1


def test_backfill_rep_counts_a_message_once_when_it_matches_two_addresses(
    gmail_env, monkeypatch
) -> None:
    """同じメールが複数アドレスのバッチでヒットしても1回しか取りに行かない。"""
    gmail = _FakeGmail(
        search_results={
            "lead@client.example.com": ["m1"],
            "other@client.example.com": ["m1"],
        },
        messages={"m1": _inbound_message()},
    )
    db = _FakeDb()

    result = _run(
        gmail,
        db,
        monkeypatch,
        contact_index={"lead@client.example.com": "cnt-1", "other@client.example.com": "cnt-2"},
        apply=True,
    )

    assert result.found_messages == 1
    assert gmail.get_calls == ["m1"]
    assert result.inserted == 1


def test_backfill_rep_skips_deleted_messages_without_counting_an_error(
    gmail_env, monkeypatch
) -> None:
    """404（完全削除済み等）は正常に起こりうる。日次同期側と同じ扱いで黙って飛ばす。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1"]},
        get_errors={"m1": GmailApiError(404, "not found")},
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.errors == 0
    assert result.matched == 0
    assert db.inserted == []


def test_backfill_rep_counts_other_api_errors(gmail_env, monkeypatch) -> None:
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1", "m2"]},
        messages={"m2": _outbound_message()},
        get_errors={"m1": GmailApiError(500, "boom")},
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.errors == 1
    # 1通の失敗で残りが止まらない。
    assert result.inserted == 1


def test_backfill_rep_counts_search_failures_without_stopping(gmail_env, monkeypatch) -> None:
    gmail = _FakeGmail(search_error_on={"lead@client.example.com"})
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.errors == 1
    assert result.found_messages == 0


def test_backfill_rep_ignores_messages_not_matching_any_contact(gmail_env, monkeypatch) -> None:
    """連絡先DBに無い相手とのメールは記録しない。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1"]},
        messages={
            "m1": _FakeMessage(
                id="m1",
                from_header="Someone <stranger@example.org>",
                to_header="Rep <rep@cnctor.jp>",
            )
        },
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.fetched == 1
    assert result.matched == 0
    assert db.inserted == []


def test_backfill_rep_ignores_internal_only_messages(gmail_env, monkeypatch) -> None:
    """社内同士のメールは連絡先候補にならない。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1"]},
        messages={
            "m1": _FakeMessage(
                id="m1",
                from_header="Colleague <other@cnctor.jp>",
                to_header="Rep <rep@cnctor.jp>",
            )
        },
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.matched == 0


def test_backfill_rep_marks_inserted_ids_as_existing_for_the_next_rep(
    gmail_env, monkeypatch
) -> None:
    """担当が複数いるとき、同じメールを2人分入れようとしない。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1"]},
        messages={"m1": _inbound_message()},
    )
    db = _FakeDb()
    existing: set[str] = set()

    _run(gmail, db, monkeypatch, existing_ids=existing, apply=True)

    assert existing == {"m1"}


# --- main()（担当の絞り込みと打ち切り、2026-09-03） -------------------------------------------


class _FakeConnection:
    def __init__(self, rep_email: str) -> None:
        self.rep_email = rep_email
        self.refresh_token_enc = "enc"


class _MainFakeDb(_FakeDb):
    def __init__(self, connections: list[_FakeConnection]) -> None:
        super().__init__()
        self._connections = connections

    def fetch_existing_message_ids(self) -> set[str]:
        return set()

    def fetch_oldest_email_sent_at(self):
        # 通常同期が面倒を見ている期間の始まり（＝ここより前だけを取り込む）
        return datetime(2026, 8, 25, 5, 11, tzinfo=timezone.utc)

    def list_gmail_connections(self) -> list[_FakeConnection]:
        return list(self._connections)


def _patch_main(monkeypatch, *, index, connections, gmail=None):
    monkeypatch.setattr(mod, "load_or_build_contact_index", lambda cache: index)
    monkeypatch.setattr(mod, "db", _MainFakeDb(connections))
    monkeypatch.setattr(mod, "decrypt_token", lambda enc: "rt")
    monkeypatch.setattr(mod, "gmail_client", gmail or _FakeGmail())


def test_main_stops_when_no_contact_has_an_email_address(monkeypatch) -> None:
    _patch_main(monkeypatch, index={}, connections=[_FakeConnection("rep@cnctor.jp")])
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py"])

    assert mod.main() == 1


def test_main_stops_when_no_rep_matches(monkeypatch) -> None:
    _patch_main(
        monkeypatch,
        index={"lead@client.example.com": "cnt-1"},
        connections=[_FakeConnection("rep@cnctor.jp")],
    )
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py", "--rep", "other@cnctor.jp"])

    assert mod.main() == 1


def test_main_filters_reps_case_insensitively(gmail_env, monkeypatch) -> None:
    processed: list[str] = []

    def fake_backfill_rep(**kwargs):
        processed.append(kwargs["rep_email"])
        return mod.RepResult(kwargs["rep_email"], 0, 0, 0, 0, 0, 0, 0, 0, 0)

    _patch_main(
        monkeypatch,
        index={"lead@client.example.com": "cnt-1"},
        connections=[_FakeConnection("Rep@Cnctor.jp"), _FakeConnection("other@cnctor.jp")],
    )
    monkeypatch.setattr(mod, "backfill_rep", fake_backfill_rep)
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py", "--rep", "rep@cnctor.jp"])

    assert mod.main() == 0
    assert processed == ["Rep@Cnctor.jp"]


def test_main_limit_addresses_shrinks_the_search_target(gmail_env, monkeypatch) -> None:
    """★ 最初の1回を小さく試すためのオプション。3万件を投げる前の安全弁。"""
    seen_index: list[dict[str, str]] = []

    def fake_backfill_rep(**kwargs):
        seen_index.append(kwargs["contact_index"])
        return mod.RepResult(kwargs["rep_email"], 0, 0, 0, 0, 0, 0, 0, 0, 0)

    _patch_main(
        monkeypatch,
        index={"a@x.com": "c1", "b@x.com": "c2", "c@x.com": "c3"},
        connections=[_FakeConnection("rep@cnctor.jp")],
    )
    monkeypatch.setattr(mod, "backfill_rep", fake_backfill_rep)
    monkeypatch.setattr(
        "sys.argv", ["backfill_gmail_history.py", "--limit-addresses", "2"]
    )

    assert mod.main() == 0
    assert seen_index == [{"a@x.com": "c1", "b@x.com": "c2"}]


def test_main_defaults_to_dry_run(gmail_env, monkeypatch) -> None:
    """`--apply`を付けなければ backfill_rep には apply=False が渡る。"""
    applied: list[bool] = []

    def fake_backfill_rep(**kwargs):
        applied.append(kwargs["apply"])
        return mod.RepResult(kwargs["rep_email"], 0, 0, 0, 0, 0, 0, 0, 0, 0)

    _patch_main(
        monkeypatch,
        index={"a@x.com": "c1"},
        connections=[_FakeConnection("rep@cnctor.jp")],
    )
    monkeypatch.setattr(mod, "backfill_rep", fake_backfill_rep)
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py"])

    assert mod.main() == 0
    assert applied == [False]


def test_main_keeps_going_when_one_rep_fails(gmail_env, monkeypatch) -> None:
    """1名の失敗が他の担当を止めない（sync_all()と同じ方針）。"""
    processed: list[str] = []

    def fake_backfill_rep(**kwargs):
        processed.append(kwargs["rep_email"])
        if kwargs["rep_email"] == "ng@cnctor.jp":
            raise RuntimeError("token expired")
        return mod.RepResult(kwargs["rep_email"], 0, 0, 0, 0, 0, 0, 0, 0, 0)

    _patch_main(
        monkeypatch,
        index={"a@x.com": "c1"},
        connections=[_FakeConnection("ng@cnctor.jp"), _FakeConnection("ok@cnctor.jp")],
    )
    monkeypatch.setattr(mod, "backfill_rep", fake_backfill_rep)
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py"])

    # 1名が落ちているので成功では終わらせない（rc=0を信用しない、という運用ルール）。
    assert mod.main() == 2
    assert processed == ["ng@cnctor.jp", "ok@cnctor.jp"]


def test_backfill_rep_inserts_in_chunks(gmail_env, monkeypatch) -> None:
    """数万件を1トランザクションで投げない（Geminiレビュー指摘、2026-09-03）。"""
    ids = [f"m{i}" for i in range(1200)]
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ids},
        messages={i: _inbound_message(i) for i in ids},
    )

    class _CountingDb(_FakeDb):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        def insert_email_logs(self, rows) -> int:
            self.batch_sizes.append(len(rows))
            return super().insert_email_logs(rows)

    db = _CountingDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert db.batch_sizes == [500, 500, 200]
    assert result.inserted == 1200


def test_backfill_rep_resolves_contacts_case_insensitively(gmail_env, monkeypatch) -> None:
    """索引は小文字。宛先が大文字混じりでも引けること（Geminiレビュー指摘、2026-09-03）。"""
    gmail = _FakeGmail(
        search_results={"lead@client.example.com": ["m1"]},
        messages={
            "m1": _FakeMessage(
                id="m1",
                from_header="Lead <LEAD@Client.Example.com>",
                to_header="Rep <rep@cnctor.jp>",
            )
        },
    )
    db = _FakeDb()

    result = _run(gmail, db, monkeypatch, apply=True)

    assert result.matched == 1
    assert db.inserted[0].contact_page_id == "cnt-1"


def test_main_returns_non_zero_when_any_message_failed(gmail_env, monkeypatch) -> None:
    """★ 取りこぼしがあるのに rc=0 で終わらない（ChatGPTレビュー指摘、2026-09-03）。"""

    def fake_backfill_rep(**kwargs):
        return mod.RepResult(kwargs["rep_email"], 0, 0, 0, 0, 0, 0, 0, 0, errors=3)

    _patch_main(
        monkeypatch,
        index={"a@x.com": "c1"},
        connections=[_FakeConnection("rep@cnctor.jp")],
    )
    monkeypatch.setattr(mod, "backfill_rep", fake_backfill_rep)
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py"])

    assert mod.main() == 2


def test_main_rejects_a_malformed_before(monkeypatch) -> None:
    _patch_main(
        monkeypatch,
        index={"a@x.com": "c1"},
        connections=[_FakeConnection("rep@cnctor.jp")],
    )
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py", "--before", "2026/08/25"])

    assert mod.main() == 1


def test_main_stops_when_email_log_is_empty_and_no_before_given(gmail_env, monkeypatch) -> None:
    """打ち切り日を決められないまま流さない（通常同期と重なる危険があるため）。"""

    class _EmptyDb(_MainFakeDb):
        def fetch_oldest_email_sent_at(self):
            return None

    monkeypatch.setattr(mod, "load_or_build_contact_index", lambda cache: {"a@x.com": "c1"})
    monkeypatch.setattr(mod, "db", _EmptyDb([_FakeConnection("rep@cnctor.jp")]))
    monkeypatch.setattr(mod, "decrypt_token", lambda enc: "rt")
    monkeypatch.setattr(mod, "gmail_client", _FakeGmail())
    monkeypatch.setattr("sys.argv", ["backfill_gmail_history.py"])

    assert mod.main() == 1


def test_load_or_build_contact_index_rebuilds_a_stale_cache(tmp_path, monkeypatch) -> None:
    """古いキャッシュは「取りこぼし」ではなく「別人の履歴として保存」を招く。"""
    import json

    cache = tmp_path / "index.json"
    cache.write_text(
        json.dumps(
            {
                "built_at": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
                "index": {"old@example.com": "cnt-old"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "build_contact_index", lambda: {"new@example.com": "cnt-new"})

    index = load_or_build_contact_index(str(cache))

    assert index == {"new@example.com": "cnt-new"}


def test_load_or_build_contact_index_rebuilds_an_unreadable_cache(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "index.json"
    cache.write_text("{壊れている", encoding="utf-8")
    monkeypatch.setattr(mod, "build_contact_index", lambda: {"a@example.com": "cnt-1"})

    assert load_or_build_contact_index(str(cache)) == {"a@example.com": "cnt-1"}
