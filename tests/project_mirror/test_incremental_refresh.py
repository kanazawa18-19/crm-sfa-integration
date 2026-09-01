"""案件ミラーの分割実行（2026-09-01）。

案件管理DBは **26,017件**（1万件の壁を越えて数え直した実数）あり、
`refresh_all_projects()`のように全件取り切ってから書く作りでは
Vercelの実行上限300秒に収まらない。
**「1万件で静かに切れる」を直すと、今度は「時間切れで何もしない」になる。**

取引先名インデックス側（`tests/relation_sync/test_incremental_refresh.py`）と
同じ形のテスト。案件ミラー固有の点は「掃除の前に急減を確かめる」「1回ぶんの取得の
中身が壊れていたら書かずに止まる」の2つ。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.project_mirror import sync as sync_module

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _page(i: int, *, with_required: bool = True) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "案件名": {"type": "title", "title": [{"plain_text": f"案件{i}"}]},
        "営業ステータス": {"type": "select", "select": {"name": "商談中"}},
    }
    if not with_required:
        properties = {"案件名": {"type": "title", "title": []}}
    return {
        "id": f"page-{i}",
        "created_time": f"2026-09-01T00:00:{i // 500:02d}.000Z",
        "last_edited_time": "2026-09-01T00:00:00.000Z",
        "properties": properties,
    }


class _FakeNotion:
    """1クエリ1万件で打ち切る本物の振る舞い（`_notion_paging`のテストと同じ）。"""

    CAP = 10_000

    def __init__(
        self, total: int, *, with_required: bool = True, same_created_time: bool = False
    ) -> None:
        self.pages = [_page(i, with_required=with_required) for i in range(total)]
        if same_created_time:
            # 一括移行でタイムスタンプが固まっている状態。キーセット方式が前へ進めない。
            for p in self.pages:
                p["created_time"] = "2026-09-01T00:00:00.000Z"

    def query_raw(self, body: dict[str, Any]) -> dict[str, Any]:
        rows = self.pages
        f = body.get("filter") or {}
        for cond in f.get("and", [f]) if f else []:
            after = (cond.get("created_time") or {}).get("on_or_after")
            if after:
                rows = [p for p in rows if p["created_time"] >= after]
        cursor = int(body.get("start_cursor") or 0)
        window = rows[: self.CAP]
        chunk = window[cursor : cursor + body["page_size"]]
        nxt = cursor + len(chunk)
        return {
            "results": chunk,
            "has_more": nxt < len(window),
            "next_cursor": str(nxt) if nxt < len(window) else None,
        }

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def query_all_pages(self) -> list[dict[str, Any]]:
        raise NotImplementedError


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """しおりとDB書き込みをメモリ上に置き換える。"""
    state: dict[str, Any] = {
        "cursor": None,
        "upserted": {},
        "swept": None,
        "alerts": [],
        "dms": [],
        # 一巡が終わったときの急減チェックが読む件数。
        # `stale_count` は「掃除で消える行数」。既定は0＝全部が今回の一巡で触れられた健全な状態。
        "total_count": 0,
        "stale_count": None,
    }

    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: object())
    monkeypatch.setattr(sync_module, "release_refresh_lock", lambda conn: None)
    monkeypatch.setattr(
        sync_module,
        "load_cursor",
        lambda name: state["cursor"]
        or sync_module.SyncCursor(name=name, watermark=None, pass_started_at=NOW),
    )
    monkeypatch.setattr(sync_module, "save_cursor", lambda c: state.update(cursor=c))
    monkeypatch.setattr(sync_module, "clear_cursor", lambda name: state.update(cursor=None))

    def _upsert(rows: list[dict[str, Any]], *, synced_at: datetime) -> None:
        for r in rows:
            state["upserted"][r["notion_page_id"]] = r
        # テストが「既にN件入っている」と置いた値は保つ（`max`）。
        state["total_count"] = max(state["total_count"], len(state["upserted"]))

    def _count(*, stale_before: datetime | None = None) -> int:
        if stale_before is None:
            return state["total_count"]
        if state["stale_count"] is not None:
            return state["stale_count"]
        # 既定は「今回の一巡が全部触れた」＝消える行は無い。
        return max(0, state["total_count"] - len(state["upserted"]))

    monkeypatch.setattr(sync_module, "upsert_projects", _upsert)
    monkeypatch.setattr(
        sync_module, "sweep_projects", lambda *, before: state.update(swept=before) or 0
    )
    monkeypatch.setattr(sync_module, "get_project_count", _count)
    monkeypatch.setattr(
        sync_module, "_notify_slack_alert", lambda m, **kw: state["alerts"].append(m)
    )
    monkeypatch.setattr(
        sync_module, "_notify_managers_slack_dm", lambda m, **kw: state["dms"].append(m)
    )
    return state


def _run(notion: _FakeNotion, **kwargs: Any) -> dict[str, Any]:
    return sync_module.refresh_projects_incrementally(
        notion_client=notion, user_directory=None, **kwargs
    )


def test_one_run_stops_within_the_budget_and_leaves_a_bookmark(store) -> None:
    notion = _FakeNotion(total=25_000)

    result = _run(notion, time_budget_seconds=0)

    assert result["completed"] is False
    assert store["cursor"] is not None, "しおりが残っていない"
    assert store["swept"] is None, "**一巡の途中で掃除してはいけない**"
    assert 0 < len(store["upserted"]) < 25_000


def test_repeated_runs_eventually_cover_everything(store) -> None:
    notion = _FakeNotion(total=25_000)

    # 時間予算を0にすると1回あたりの前進が細かくなる（周の途中でも中断するため）。
    # 実運用の予算は170秒なので1回でずっと多く進む。ここは回数で殴って一巡させる。
    for _ in range(400):
        result = _run(notion, time_budget_seconds=0)
        if result.get("completed"):
            break

    assert len(store["upserted"]) == 25_000
    assert store["swept"] == NOW, "一巡し終えたら掃除する"
    assert store["cursor"] is None, "一巡し終えたらしおりを捨てる"


def test_sweep_uses_the_time_the_pass_started(store) -> None:
    """掃除の基準は「一巡を始めた時刻」。**途中の時刻を使うと取り込み済みの行まで消える。**"""
    notion = _FakeNotion(total=500)

    _run(notion)

    assert store["swept"] == NOW


def test_upsert_is_stamped_with_the_pass_start_not_now(store, monkeypatch) -> None:
    """UPSERTの`synced_at`も一巡の開始時刻。ズレると掃除が取り込み済みの行を消す。"""
    seen: list[datetime] = []

    def _upsert(rows: list[dict[str, Any]], *, synced_at: datetime) -> None:
        seen.append(synced_at)
        for r in rows:
            store["upserted"][r["notion_page_id"]] = r
        store["total_count"] = max(store["total_count"], len(store["upserted"]))

    monkeypatch.setattr(sync_module, "upsert_projects", _upsert)

    # 時間予算を0にすると1回あたりの前進が細かくなる（周の途中でも中断するため）。
    # 実運用の予算は170秒なので1回でずっと多く進む。ここは回数で殴って一巡させる。
    for _ in range(400):
        if _run(_FakeNotion(total=6_000), time_budget_seconds=0).get("completed"):
            break

    assert seen, "UPSERTが1度も呼ばれていない"
    assert set(seen) == {NOW}


def test_skips_when_another_run_is_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: None)

    result = _run(_FakeNotion(total=10))

    assert result["skipped"] == "already_running"


def test_releases_the_lock_even_when_notion_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[Any] = []
    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: "conn")
    monkeypatch.setattr(sync_module, "release_refresh_lock", lambda c: released.append(c))
    monkeypatch.setattr(
        sync_module, "load_cursor", lambda name: sync_module.SyncCursor(name, None, NOW)
    )

    class _Broken(_FakeNotion):
        def query_raw(self, body: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("Notionに届かない")

    with pytest.raises(RuntimeError):
        _run(_Broken(total=10))

    assert released == ["conn"], "例外でもロックを解放すること"


# --- 案件ミラー固有のガード -------------------------------------------------------------


def test_broken_slice_is_not_written_and_does_not_advance_the_bookmark(store) -> None:
    """必須プロパティが欠落した取得は**書かない・しおりも進めない**。

    2026-08-26に「行数は正常だが中身が丸ごと欠落」した10000件で本番のダッシュボードが
    全て0件になった。分割実行では1回ぶんの取得ごとにこれを見る。
    """
    notion = _FakeNotion(total=3_000, with_required=False)

    result = _run(notion, time_budget_seconds=0)

    assert result["skipped"] == "insufficient_required_properties"
    assert store["upserted"] == {}, "壊れたデータを書いてはいけない"
    assert store["cursor"] is None, "しおりを進めてはいけない（次回また取り直す）"
    assert store["swept"] is None
    assert store["alerts"], "通知まで上げること（ログ1行では気づけない）"
    assert store["dms"], (
        "**マネージャーDMまで送ること。** SLACK_WEBHOOK_URL_ALERT は本番未設定で、"
        "webhook側だけでは誰にも届かない（2026-09-01のレビュー指摘）"
    )


def test_a_tiny_slice_is_still_stopped_when_every_row_is_broken(store) -> None:
    """**小さいスライスも素通りさせない**（2026-09-01、Gemini・ChatGPTが独立に指摘）。

    分割実行では一巡の最後に20件未満のスライスが正常に出るため、件数の下限だけで
    判定すると、そこが検査なしの穴になる。全件が欠落しているなら小さくても止める。
    """
    notion = _FakeNotion(total=5, with_required=False)

    result = _run(notion)

    assert result["skipped"] == "insufficient_required_properties"
    assert store["upserted"] == {}, "壊れたデータを書いてはいけない"


def test_a_tiny_slice_with_one_blank_row_still_goes_through(store) -> None:
    """ただし**数件の標本に9割の閾値は掛けない。**

    たまたま1件空欄なだけで止めると、しおりを進めないので一巡が永久に終わらない。
    小さいスライスでは「全件欠落」のときだけ止める。
    """
    notion = _FakeNotion(total=5)
    notion.pages[0]["properties"]["営業ステータス"] = {"type": "select", "select": None}

    result = _run(notion)

    assert "skipped" not in result, "1件空欄なだけで一巡を止めている"
    assert len(store["upserted"]) == 5


def test_sweep_is_aborted_when_it_would_delete_more_than_half(store) -> None:
    """掃除が既存の半分を超えて消すなら止める（部分取得の疑い）。"""
    notion = _FakeNotion(total=100)
    store["total_count"] = 26_000
    store["stale_count"] = 26_000 - 100  # 100件だけ触れた＝残りは全部消える

    result = _run(notion)

    assert result["skipped"] == "suspected_partial_fetch"
    assert store["swept"] is None, "既存データを消してはいけない"
    assert store["cursor"] is None, "しおりを捨てて次回は先頭からやり直す"
    assert store["alerts"]


def test_sweep_is_aborted_when_it_would_delete_everything(store) -> None:
    """1件も触れていない一巡で掃除すると、ミラーが全消失する（2026-08-25の事故）。"""
    notion = _FakeNotion(total=0)
    store["total_count"] = 10
    store["stale_count"] = 10  # 1件も触れていない＝全部消える

    result = _run(notion)

    assert result["skipped"] == "suspected_partial_fetch"
    assert store["swept"] is None

# --- 他モデルレビュー（Gemini）で出た経路 -------------------------------------------------


def test_a_stalled_keyset_never_reaches_the_sweep(store) -> None:
    """**取りこぼしたまま掃除に進ませない。**

    同じ作成日時が1周の上限ぶん並ぶとキーセット方式は前へ進めない。以前はそこで
    completed=True を返しており、呼び出し元が「取り切った」と誤認して掃除に進み、
    **まだ見ていない後半の行が全部消える**経路になっていた（2026-09-01、Gemini指摘）。
    """
    notion = _FakeNotion(total=15_000, same_created_time=True)

    result = _run(notion)

    assert result["skipped"] == "keyset_stalled"
    assert result["completed"] is False
    assert store["swept"] is None, "取りこぼしているのに掃除している"
    assert store["cursor"] is not None, "しおりを残して人が直せるようにすること"
    assert store["dms"], "自力では回復しないので人へ届けること"
    assert store["upserted"], "取れた分は反映してよい"


def test_a_legitimate_bulk_delete_can_be_approved_once(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**正当な大量削除で掃除が永久に止まるのを、運用者が1回だけ解除できる。**

    件数だけでは部分取得と正当な削除を見分けられない。解除しないと、消えたはずの行が
    残り続けて総数が大きいままになり、**二度と掃除されない**（2026-09-01、Gemini指摘）。
    """
    notion = _FakeNotion(total=100)
    store["total_count"] = 26_000
    store["stale_count"] = 26_000 - 100  # 100件だけ触れた＝残りは全部消える

    assert _run(notion)["skipped"] == "suspected_partial_fetch"
    assert store["swept"] is None

    monkeypatch.setenv("PROJECT_MIRROR_ALLOW_SHRINK", "true")
    store["total_count"] = 26_000
    store["stale_count"] = 26_000 - 100  # 100件だけ触れた＝残りは全部消える

    result = _run(notion)

    assert "skipped" not in result, "承認したのに掃除を止めている"
    assert store["swept"] == NOW


def test_the_guard_counts_what_the_sweep_would_delete(store) -> None:
    """**生存の数え方ではなく、掃除が消す行数で判断する**（2026-09-01）。

    「この一巡が触れた行数」の定義で他モデルのレビューが割れた。
    ChatGPTは「`>=`だと一巡の最中のWebhook更新が混ざって検知が鈍る」と言い、
    Geminiは「等号だと生きている行を数え落として誤検知する」と言った。**どちらも正しい。**
    破壊的操作が消す行数（`syncedAt < 基準時刻`）を直接数えれば、この議論自体が要らなくなる。
    Webhookが更新した行は`syncedAt`が基準時刻より未来なので、そもそも消えない。
    """
    import inspect
    from src.project_mirror import db as db_module

    src = inspect.getsource(db_module.get_project_count)
    assert '"syncedAt" < %s' in src, (
        "掃除が消す行数を数えていない。生存側を数えると、Webhook更新の扱いで"
        "検知が鈍るか誤検知するかのどちらかになる"
    )
    assert '"syncedAt" >= %s' not in src
    assert '"syncedAt" = %s' not in src


def test_the_round_limit_can_be_raised_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停滞したとき、運用者がコードを直さずに突破できること（2026-09-01、Gemini Pro指摘）。

    同じ作成日時が1周の上限ぶん並ぶと前へ進めない。Notion APIにタイブレーカーが無いので
    コード側では自力回復できず、**上限を上げるしか手が無い。**
    そのたびにデプロイが要るのでは、深夜に止まったとき動けない。
    """
    assert sync_module._round_limit() == sync_module._ROUND_LIMIT

    monkeypatch.setenv("PROJECT_MIRROR_ROUND_LIMIT", "5000")
    assert sync_module._round_limit() == 5_000

    # 壊れた値は既定へ落とす（設定ミスで同期が止まる方が困る）。
    monkeypatch.setenv("PROJECT_MIRROR_ROUND_LIMIT", "たくさん")
    assert sync_module._round_limit() == sync_module._ROUND_LIMIT
    monkeypatch.setenv("PROJECT_MIRROR_ROUND_LIMIT", "0")
    assert sync_module._round_limit() == sync_module._ROUND_LIMIT
