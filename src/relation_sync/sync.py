"""取引先マスターDB（Notion）→ ClientNameIndex（Postgres）への同期処理本体（2026-08-25）。

データの正本は引き続きNotionであり、本モジュールは`src/project_mirror/sync.py`と同じ
3つのエントリポイントを提供する。

- `sync_client_name_to_index()`: Notion Webhook経由の1件更新用。
- **`refresh_client_names_incrementally()`: 夜間reconciliation cronの現役の入口**
  （2026-09-01〜）。時間予算で区切って中断し、しおり（`SyncCursor`）に続きを残す分割実行。
  取引先マスターは102,799件あり、全件取得だけで約18分かかるため1回では終わらない。
- `refresh_all_client_names()`: **ローカルからの初回バックフィル専用**
  （`scripts/backfill_client_name_index.py`）。**夜間cronからはもう呼ばれていない。**

いずれも`src/relation_sync/resolve.py`（kintone等からのリレーション解決）が参照する
`ClientNameIndex`テーブルを最新化するためだけのものであり、双方向同期パイプライン
（Dispatcher/SyncEvent/IdMappingStore）には一切関与しない。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol

import requests

from src.migration.zoho_client_master import normalize_company_name_strong
import dataclasses

from src.sync_engine.clients._notion_paging import query_keyset_slice
from src.sync_engine.sync_cursor import SyncCursor, clear_cursor, load_cursor, save_cursor
from src.relation_sync.db import (
    get_client_name_count,
    release_refresh_lock,
    try_acquire_refresh_lock,
    upsert_client_name,
    sweep_client_names,
    upsert_client_names,
    upsert_client_names_and_sweep,
)
from src.sync_engine.clients.notion_client import parse_notion_property_value

logger = logging.getLogger(__name__)

# 新規取得件数が既存インデックス件数のこの割合を下回った場合、部分取得の疑いが強いとして
# sweepを中止する(project_mirror/sync.pyの_MIN_SYNC_RATIOと同じ安全装置)。
_MIN_SYNC_RATIO = 0.5

# 取引先マスターDBのtitleプロパティ名(src/db_schema/client_master.py参照)。
_TITLE_PROPERTY_NAME = "取引先名"


class ClientMasterNotionClient(Protocol):
    """本モジュールが要求するNotionクライアントの最小インターフェース
    (`src.project_mirror.sync.ProjectMirrorNotionClient`と同じ形)。"""

    def get_raw_page(self, page_id: str) -> Mapping[str, Any]: ...

    def query_all_pages(self) -> list[dict[str, Any]]: ...

    #: 分割実行（`refresh_client_names_incrementally`）が使う。Database Queryを1回だけ叩き、
    #: ページングは`src/sync_engine/clients/_notion_paging.py`側が行う。
    #: 戻り値は`Mapping`（`ProjectMirrorNotionClient`と型を揃える。実装は`dict`を返すが、
    #: このプロトコルは読み取りしか要求しない）。
    def query_raw(self, body: dict[str, Any]) -> Mapping[str, Any]: ...


def _page_to_index_row(page: Mapping[str, Any]) -> dict[str, Any] | None:
    """取引先マスターDBの1ページ(Notion API生JSON)を、ClientNameIndexの1行へ変換する。

    titleプロパティ("取引先名")が空の場合はNoneを返す(`src.migration.notion_dedupe.
    fetch_client_master_snapshots()`と同じ扱い。titleは必須プロパティのため通常は
    発生しないが、万一空だった場合に空文字列を正規化キーとしてインデックス化しても
    検索用途としては意味を持たないため)。
    """
    props = page.get("properties") or {}
    title_prop = props.get(_TITLE_PROPERTY_NAME)
    title = parse_notion_property_value(title_prop) if title_prop else None
    if not title:
        logger.warning(
            "relation_sync: page_id=%r の取引先名(title)が空のためClientNameIndexへの反映を"
            "スキップします",
            page.get("id"),
        )
        return None
    return {
        "notion_page_id": page["id"],
        "normalized_name": normalize_company_name_strong(title),
        "raw_name": title,
    }


def sync_client_name_to_index(
    properties: Mapping[str, Any], page_id: str, *, notion_client: ClientMasterNotionClient
) -> None:
    """取引先マスターDBの1ページをClientNameIndexへ反映する(Notion Webhook経由)。

    `properties`（`SyncEvent.properties`相当）は`sync_project_to_mirror()`と型を揃えるためだけ
    に受け取り、実際には使わない（`notion_webhook.handler_with_proxy`の`project_mirror_sync`と
    同じ`Callable[[Mapping[str, Any], str], Any]`シグネチャに合わせている）。
    `notion_client.get_raw_page(page_id)`でページ全体を取得して変換する。
    例外はこの関数では握りつぶさない(呼び出し元がWebhook全体を失敗させない判断を行う)。
    """
    page = notion_client.get_raw_page(page_id)
    row = _page_to_index_row(page)
    if row is None:
        return
    upsert_client_name(row)


def refresh_all_client_names(
    *, notion_client: ClientMasterNotionClient
) -> dict[str, Any]:
    """取引先マスターDB全件をインデックスへ反映する（**ローカルからの初回バックフィル専用**）。

    **夜間reconciliation cronはこの関数を使っていない**（2026-09-01〜。全件取得に約18分
    かかりVercelの300秒に収まらないため`refresh_client_names_incrementally()`へ切り替えた）。
    実行時間の上限が無い場所からのみ呼ぶこと。

    `notion_client.query_all_pages()`で全件取得してから変換し、
    `upsert_client_names_and_sweep()`を1回呼ぶ。全件取得が完了するまでDB書き込みを開始しない
    (`refresh_all_projects()`と同じ設計)。

    実行開始時にPostgresアドバイザリロックの取得を試み、既に別プロセスが実行中の場合は
    即座にスキップする(project_mirror/sync.pyのrefresh_all_projects()と同じ多重実行防止)。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_all_client_names: 既に別プロセスが実行中と判断したためスキップします"
            "（pg_try_advisory_lockの取得に失敗）"
        )
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}
    try:
        pages = notion_client.query_all_pages()
        rows = [row for row in (_page_to_index_row(page) for page in pages) if row is not None]

        # `query_all_pages()`側のページング中断（部分取得）に対する保護
        # (project_mirror/sync.pyのrefresh_all_projects()と同じ安全装置)。
        current_count = get_client_name_count()
        if current_count >= 20 and len(rows) < current_count * _MIN_SYNC_RATIO:
            message = (
                f"refresh_all_client_names: 新規取得件数({len(rows)}件)が既存インデックス件数"
                f"({current_count}件)より大幅に少ないため、部分取得の疑いがありsweepを"
                "中止しました（既存データは変更していません）。"
            )
            logger.error(message)
            _notify_slack_alert(message, source="refresh_all_client_names")
            _notify_managers_slack_dm(message, source="refresh_all_client_names")
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "skipped": "suspected_partial_fetch",
            }

        deleted_count = upsert_client_names_and_sweep(rows)
        return {"synced_count": len(rows), "deleted_count": deleted_count}
    finally:
        release_refresh_lock(lock_conn)


#: このしおりの名前（`SyncCursor`テーブルのキー）。
CURSOR_NAME = "client_name_index"

#: **取得に**使ってよい秒数。Vercelの実行上限は300秒。
#: この予算は`query_keyset_slice()`の取得だけに掛かり、そのあとの行の変換・UPSERT・
#: 件数の数え直し・しおりの保存は予算の外側で走る（2026-09-01、ChatGPTのレビュー指摘）。
#: 300秒で強制終了されるとしおりを保存できず、**翌晩も同じ区間をやり直して一巡が進まない**
#: （書き込み自体は冪等なのでデータは壊れないが、永久に終わらなくなる）。
#: 残りを書き込み側の余裕として空けておく。
#: **`src/project_mirror/sync.py`にも同名の定数がある。片方だけ変えないこと。**
DEFAULT_TIME_BUDGET_SECONDS = 170.0

#: 1周で取る件数。中断の粒度になる（小さいほど時間予算を守りやすい）。
_ROUND_LIMIT = 2_000


def refresh_client_names_incrementally(
    *,
    notion_client: ClientMasterNotionClient,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """取引先マスターを**何回かに分けて**インデックスへ反映する（2026-09-01）。

    ■ なぜ分けるのか

    取引先マスターDBは **102,799件** あり、全件取得だけで約18分かかる。
    Vercelの実行上限は300秒なので、1回では終わらない。
    **「1万件で静かに切れる」を直したら、今度は「時間切れで何もしない」になる。**

    そこで時間予算で区切って中断し、しおり（`SyncCursor`）に「どこまで取ったか」を
    残す。次の実行が続きから再開し、何回かで一巡する。

    ■ 掃除は一巡し終えたときだけ

    掃除は「今回見なかった行を消す」やり方。途中で掃除すると**まだ見ていないだけの行を
    消してしまう**（ProjectMirrorを全消失させた事故と同じ形）。
    一巡を始めた時刻を覚えておき、一巡し終えたときだけそれより古い行を消す。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_client_names_incrementally: 既に別プロセスが実行中と判断したため"
            "スキップします（pg_try_advisory_lockの取得に失敗）"
        )
        return {
            "synced_count": 0,
            "deleted_count": 0,
            "completed": False,
            "skipped": "already_running",
        }
    try:
        cursor = load_cursor(CURSOR_NAME)
        # 運用者が朝ログで進み具合を判断できるようにする（`project_mirror/sync.py`と同じ）。
        if cursor.is_new_pass:
            logger.info(
                "refresh_client_names_incrementally: 新しい一巡を始めます（基準時刻=%s）",
                cursor.pass_started_at,
            )
        else:
            logger.info(
                "refresh_client_names_incrementally: 前回の続き（%s 以降）から再開します"
                "（この一巡の基準時刻=%s）",
                cursor.watermark,
                cursor.pass_started_at,
            )

        def _post(body: dict[str, Any]) -> dict[str, Any]:
            return notion_client.query_raw(body)

        slice_ = query_keyset_slice(
            _post,
            watermark=cursor.watermark,
            round_limit=_ROUND_LIMIT,
            time_budget_seconds=time_budget_seconds,
            label=CURSOR_NAME,
        )
        rows = [
            row for row in (_page_to_index_row(page) for page in slice_.pages) if row is not None
        ]

        # **取りこぼしたまま掃除へ進ませない**（2026-09-01、Geminiレビュー指摘）。
        # 同じ作成日時が1周の上限ぶん並ぶと、キーセット方式は前へ進めなくなる。
        # 以前はそこで completed=True を返しており、呼び出し元は「取り切った」と誤認して
        # 掃除に進み、**まだ見ていない後半の行が全部消える**経路になっていた。
        # 取得済みが全体の半分を超えていれば件数の急減チェックもすり抜ける。
        # 自力では回復しない（round_limitを上げるか、データ側の重なりを解消するしかない）ので、
        # 取れた分だけ反映して、しおりはそのまま残し、人へ届ける。
        if slice_.stalled:
            upsert_client_names(rows, synced_at=cursor.pass_started_at)
            save_cursor(dataclasses.replace(cursor, watermark=slice_.watermark))
            message = (
                f"refresh_client_names_incrementally: 同じ作成日時が1周の上限({_ROUND_LIMIT}件)ぶん並んでおり"
                f"これ以上進めません（created_time={slice_.watermark}）。**取りこぼしています。**"
                "掃除は行っていません（既存データは消していません）。"
                "_ROUND_LIMITを上げるか、作成日時の重なりを解消してください。"
                "直るまで一巡は完了しません。"
            )
            logger.error(message)
            _notify_slack_alert(message, source="refresh_client_names_incrementally")
            _notify_managers_slack_dm(message, source="refresh_client_names_incrementally")
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "completed": False,
                "skipped": "keyset_stalled",
            }

        upsert_client_names(rows, synced_at=cursor.pass_started_at)

        if not slice_.completed:
            save_cursor(dataclasses.replace(cursor, watermark=slice_.watermark))
            logger.info(
                "refresh_client_names_incrementally: 途中まで取り込みました"
                "（今回%d件 / この一巡でここまで%d件。次回 %s 以降から続けます）",
                len(rows),
                get_client_name_count(synced_since=cursor.pass_started_at),
                slice_.watermark,
            )
            return {"synced_count": len(rows), "deleted_count": 0, "completed": False}

        # 一巡し終えた。ここで初めて掃除する。ただし掃除の前に急減を確かめる
        # （2026-09-01追加。`refresh_all_client_names()`にはある部分取得ガードが
        # 分割実行版に無く、案件ミラー側と非対称だった。1回ぶんの取得件数(2,000件)と
        # 全体(102,799件)を比べても意味が無いので、「この一巡で触れた行数」＝掃除が
        # 消し残す行数で見る）。中止したときはしおりを捨てて次回は先頭からやり直す。
        total_count = get_client_name_count()
        touched_count = get_client_name_count(synced_since=cursor.pass_started_at)
        # **本当に大量削除されたときに、掃除が永久に発火しなくなるのを避ける**
        # （2026-09-01、Geminiレビュー指摘）。Notion側で正当に半分以上が消されると、
        # このチェックが毎回成立して掃除が飛ばされ、消えたはずの行が残るので
        # `total_count`は大きいまま。翌晩も翌々晩も同じ判定になり、**二度と掃除されない。**
        # 部分取得と正当な大量削除は件数だけでは見分けられないので、
        # 運用者が1回だけ許可できる逃げ道を用意する（承認したら環境変数を戻すこと）。
        allow_shrink = os.environ.get("RELATION_SYNC_ALLOW_SHRINK", "").strip().lower() == "true"
        if not allow_shrink and (
            touched_count == 0
            or (total_count >= 20 and touched_count < total_count * _MIN_SYNC_RATIO)
        ):
            message = (
                f"refresh_client_names_incrementally: 一巡で触れた件数({touched_count}件)が"
                f"既存インデックス件数({total_count}件)より大幅に少ないため、部分取得の疑いが"
                "あり掃除を中止しました（既存データは変更していません。しおりを捨てたので"
                "次回は先頭から取り直します）。**Notion側で正当に大量削除した結果であれば、"
                "環境変数 RELATION_SYNC_ALLOW_SHRINK=true を設定して1回だけ掃除を通し、"
                "その後は必ず設定を戻してください。**"
            )
            logger.error(message)
            _notify_slack_alert(message, source="refresh_client_names_incrementally")
            _notify_managers_slack_dm(message, source="refresh_client_names_incrementally")
            clear_cursor(CURSOR_NAME)
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "skipped": "suspected_partial_fetch",
                "completed": True,
            }

        deleted_count = sweep_client_names(before=cursor.pass_started_at)
        clear_cursor(CURSOR_NAME)
        logger.info(
            "refresh_client_names_incrementally: 一巡し終えました（今回%d件 / 掃除%d件）",
            len(rows),
            deleted_count,
        )
        return {"synced_count": len(rows), "deleted_count": deleted_count, "completed": True}
    finally:
        release_refresh_lock(lock_conn)


def _notify_slack_alert(message: str, *, source: str = "relation_sync") -> None:
    """`src/project_mirror/sync.py`の`_notify_slack_alert()`と同じ`SLACK_WEBHOOK_URL_ALERT`
    (運用アラートチャンネル)へ通知する。送信失敗はログのみで握りつぶす。

    **`SLACK_WEBHOOK_URL_ALERT`は本番未設定であることが判明しており、この経路は実質no-op。**
    実際に運用者へ届くのは`_notify_managers_slack_dm()`側なので、**必ず両方を呼ぶこと**
    （2026-09-01、レビュー指摘。急減チェックを足したのに通知が誰にも届かない状態だった）。

    `source`には呼び出し元の関数名を渡す。全件版と分割実行版のどちらで起きたのかが
    ログから分からないと、運用者が原因を追えないため。
    """
    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=10)
    except Exception:
        logger.exception("%s: failed to post alert to slack", source)


def _notify_managers_slack_dm(message: str, *, source: str = "relation_sync") -> None:
    """`User.isManager = true`の全ユーザーへSlack DMで通知する
    （`src/notifications/manager_dm.py`）。

    `src/project_mirror/sync.py`の同名関数と同じ理由でこちらが**主経路**。
    `SLACK_WEBHOOK_URL_ALERT`が本番未設定と判明している中で、実際に人へ届くのはこの経路だけ
    （2026-09-01追加。それまで取引先名インデックス側にはこれが無く、案件ミラー側とは
    通知の生存性が非対称だった。判定ロジックだけ揃えても、鳴らない通知では意味がない）。

    `manager_dm`はここで遅延importする（循環import回避。`project_mirror/sync.py`と同じ慣習）。
    `notify_managers()`自体が例外を握りつぶす設計だが、念のためここでも捕捉し、
    Slack通知の失敗で掃除中止の判断自体を失敗させない。
    """
    from src.notifications import manager_dm

    try:
        manager_dm.notify_managers(message, log_context=source)
    except Exception:
        logger.exception("%s: failed to notify managers via Slack DM", source)
