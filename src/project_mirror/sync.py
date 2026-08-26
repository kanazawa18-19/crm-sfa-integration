"""案件管理DB（Notion）→ ProjectMirror（Postgres）への同期処理本体（2026-08-17）。

データの正本は引き続きNotionであり、本モジュールは以下2つのエントリポイントを提供する。

- `sync_project_to_mirror()`: Notion Webhook経由の1件更新用
  （`src/sync_engine/webhook_handlers/notion_webhook.py`の`calendar_sync`/`lead_sync`と同じ
  拡張点パターン）。
- `refresh_all_projects()`: 初回バックフィル（`scripts/backfill_project_mirror.py`）・夜間
  reconciliation cronの両方から使う、案件管理DB全件のフル同期。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol

import requests

from src.api.notion_display import project_page_to_mirror_record
from src.db_schema.project import PROJECT_SCHEMA
from src.project_mirror.db import (
    get_project_count,
    release_refresh_lock,
    try_acquire_refresh_lock,
    upsert_project,
    upsert_projects_and_sweep,
)
from src.sync_engine.webhook_handlers._common import parse_iso_datetime

logger = logging.getLogger(__name__)

# 新規取得件数が既存ミラー件数のこの割合を下回った場合、部分取得(Notion側のページング
# 中断・レート制限等)の疑いが強いとしてsweepを中止する(2026-08-18、実際に発生した
# 「ミラーが全件0件になる」事故への対策)。
_MIN_SYNC_RATIO = 0.5

# ダッシュボード集計が成立するために不可欠なプロパティ。PROJECT_SCHEMA上で
# RequirementLevel.REQUIREDのプロパティ(2026-08-26時点で「案件名」「営業ステータス」の2つ)を
# そのまま使う。特に「営業ステータス」はsrc/api/dashboard_service.pyのbuild_daily_report()・
# build_member_performance()・build_manager_alerts()が`p.get(PROP_営業ステータス) is None`で
# 案件そのものを集計から除外するために使う最重要プロパティであり、これが欠落した行が
# 大量に混入すると、行数は正常でも集計結果が軒並み0件になる(2026-08-26に実際に発生した
# インシデント、docs/project_mirror_activation_note.md参照)。
_REQUIRED_PROPERTY_NAMES: tuple[str, ...] = tuple(
    p.name for p in PROJECT_SCHEMA.properties if p.is_required
)

# 取得・変換した行のうち、上記必須プロパティそれぞれが値を持つ行の割合がこれを下回った場合、
# 「行数は正常だが中身(必須プロパティ)が壊れている」疑いが強いとしてsweepを中止する
# (2026-08-26、10000件全件で主要プロパティが丸ごと欠落する事故が発生し、既存の件数ベースの
# ガード(_MIN_SYNC_RATIO/_MIN_EXPECTED_SYNCED_COUNT)ではすり抜けたための対策)。
# 「案件名」「営業ステータス」はいずれもNotion側でTITLE/REQUIRED区分のプロパティであり、
# 正常なデータであればほぼ全件に値が入っているはずなので、90%という閾値は正常な本番データを
# 誤って止めてしまわないよう十分に余裕を持たせつつ、今回のような壊滅的な欠落(実績0%)は
# 確実に検知できる水準として設定した。
_MIN_REQUIRED_PROPERTY_RATIO = 0.9

# 完全性チェックを発動させる最小行数。件数が極端に少ない場合の誤検知を避けるため、
# _MIN_SYNC_RATIOの`current_count >= 20`と同じ考え方で最小サイズを設ける。
_MIN_ROWS_FOR_COMPLETENESS_CHECK = 20


class ProjectMirrorNotionClient(Protocol):
    """本モジュールが要求するNotionクライアントの最小インターフェース。"""

    def get_raw_page(self, page_id: str) -> Mapping[str, Any]: ...

    def query_all_pages(self) -> list[dict[str, Any]]: ...


def _page_to_mirror_row(page: Mapping[str, Any], *, user_directory: Any) -> dict[str, Any]:
    record, skipped = project_page_to_mirror_record(page, user_directory)
    if skipped:
        logger.warning(
            "project_mirror: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
            PROJECT_SCHEMA.key,
            sorted(skipped),
        )
    last_edited_time = page.get("last_edited_time")
    return {
        "notion_page_id": record["notion_page_id"],
        "data": record,
        "last_edited_at": parse_iso_datetime(last_edited_time) if last_edited_time else None,
    }


def _required_property_fill_ratios(rows: list[dict[str, Any]]) -> dict[str, float]:
    """`rows`（`_page_to_mirror_row()`の戻り値のリスト）について、必須プロパティごとに
    値が設定されている(データに存在し、かつNone/空文字/空リストではない)行の割合を返す。

    `rows`が空の場合は呼び出し元(`refresh_all_projects`)側で別途空リストの扱いをするため、
    ここでは全て1.0(問題なし)として返す。
    """
    if not rows:
        return {name: 1.0 for name in _REQUIRED_PROPERTY_NAMES}
    return {
        name: sum(1 for row in rows if row["data"].get(name)) / len(rows)
        for name in _REQUIRED_PROPERTY_NAMES
    }


def sync_project_to_mirror(
    properties: Mapping[str, Any],
    page_id: str,
    *,
    notion_client: ProjectMirrorNotionClient,
    user_directory: Any,
) -> None:
    """`notion_webhook.handler_with_proxy`の第3の副作用コールバック（db_key="project"の
    SyncEventについて呼ばれる）。

    `properties`（`SyncEvent.properties`相当）は`calendar_sync`/`lead_sync`と型を揃えるためだけ
    に受け取り、実際には使わない（書き込み可能プロパティのみに絞られておりFORMULA/ROLLUPが
    欠落するため）。`notion_client.get_raw_page(page_id)`でページ全体を再取得して変換する。

    例外はこの関数では握りつぶさない（`handler_with_proxy()`側が`calendar_sync`/`lead_sync`と
    同じtry/exceptで「Webhook全体としては失敗させない」判断を行う設計に合わせる）。
    """
    page = notion_client.get_raw_page(page_id)
    row = _page_to_mirror_row(page, user_directory=user_directory)
    upsert_project(row)


def refresh_all_projects(
    *, notion_client: ProjectMirrorNotionClient, user_directory: Any
) -> dict[str, Any]:
    """案件管理DB全件をミラーへ反映する（初回バックフィル・夜間reconciliation共通）。

    `notion_client.query_all_pages()`で全件取得してから変換し、`upsert_projects_and_sweep()`を
    1回呼ぶ。全件取得が完了するまでDB書き込みを開始しない（取得の途中で失敗した場合に、
    中途半端な件数でミラーをsweepしてしまう事故を避けるため）。

    実行開始時にPostgresアドバイザリロック（`pg_try_advisory_lock`）の取得を試み、既に別
    プロセスが実行中の場合は即座にスキップする（shirokuma-secレビューWARN対応、2026-08-17。
    夜間reconciliation cronと手動バックフィルスクリプトが偶発的に重なると、後から完了した
    方が古い実行の`syncedAt`で新しいデータを上書き・sweepしてしまう恐れがあるため）。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_all_projects: 既に別プロセスが実行中と判断したためスキップします"
            "（pg_try_advisory_lockの取得に失敗）"
        )
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}
    try:
        pages = notion_client.query_all_pages()
        rows = [_page_to_mirror_row(page, user_directory=user_directory) for page in pages]

        # `query_all_pages()`は、Notion APIが`has_more=True`なのに`next_cursor`を返さない
        # という契約違反のレスポンスに遭遇した場合、例外を投げず警告ログのみでページングを
        # 打ち切り、それまでに取得できた分だけを返す設計になっている（無限ループを避ける
        # ための意図的な挙動）。このモジュールの docstring は「全件取得が完了するまで
        # DB書き込みを開始しない」ことを前提にしていたが、実際には`query_all_pages()`側の
        # この挙動により「部分取得なのに正常応答に見える」ケースがあり得る。2026-08-18、
        # 実際にこれが原因と見られる事故（ミラーが1晩で0件になった）が発生したため、
        # 新規取得件数が既存ミラー件数に比べて急減している場合はsweepを中止して既存データを
        # 保護する（既存件数が少ない場合の誤検知を避けるため、既存件数が極端に小さい時は
        # このチェック自体を素通りさせる）。
        current_count = get_project_count()
        if current_count >= 20 and len(rows) < current_count * _MIN_SYNC_RATIO:
            message = (
                f"refresh_all_projects: 新規取得件数({len(rows)}件)が既存ミラー件数"
                f"({current_count}件)より大幅に少ないため、部分取得の疑いがありsweepを"
                "中止しました（既存データは変更していません）。"
            )
            logger.error(message)
            _notify_slack_alert(message)
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "skipped": "suspected_partial_fetch",
            }

        # 「行数は正常だが中身(必須プロパティ)が壊れている」事故の検知(2026-08-26)。
        # 上のcurrent_countベースのチェックは行数の急減しか見ておらず、10000件全件の
        # UPSERTには成功しつつ各行の主要プロパティが丸ごと欠落するという壊れ方を
        # すり抜けた(docs/project_mirror_activation_note.md参照)。少数データでの誤検知を
        # 避けるため、rowsが_MIN_ROWS_FOR_COMPLETENESS_CHECK未満の場合はこのチェック自体を
        # 素通りさせる(current_count>=20のガードと同じ考え方)。
        if len(rows) >= _MIN_ROWS_FOR_COMPLETENESS_CHECK:
            fill_ratios = _required_property_fill_ratios(rows)
            insufficient = {
                name: ratio
                for name, ratio in fill_ratios.items()
                if ratio < _MIN_REQUIRED_PROPERTY_RATIO
            }
            if insufficient:
                message = (
                    f"refresh_all_projects: 取得した{len(rows)}件のうち必須プロパティの"
                    f"充足率が閾値({_MIN_REQUIRED_PROPERTY_RATIO:.0%})を下回るものがあり"
                    f"（{insufficient}）、中身が壊れている疑いがありsweepを中止しました"
                    "（既存データは変更していません）。"
                )
                logger.error(message)
                _notify_slack_alert(message)
                _notify_managers_slack_dm(message)
                return {
                    "synced_count": len(rows),
                    "deleted_count": 0,
                    "skipped": "insufficient_required_properties",
                    "required_property_fill_ratios": fill_ratios,
                }

        deleted_count = upsert_projects_and_sweep(rows)
        return {"synced_count": len(rows), "deleted_count": deleted_count}
    finally:
        release_refresh_lock(lock_conn)


def _notify_slack_alert(message: str) -> None:
    """`src/incident_detection/notify.py`の日次ダイジェストと同じ`SLACK_WEBHOOK_URL_ALERT`
    (運用アラートチャンネル)へ通知する。送信失敗はログのみで握りつぶす。

    `SLACK_WEBHOOK_URL_ALERT`は本番未設定であることが判明しており(`src/sync_engine/
    slack_notifier.py`参照)、現状は実質no-opだが、将来設定された場合に備えてこのまま残す
    (既存の`_MIN_SYNC_RATIO`ガードが使っている経路と同じ)。実際に運用者へ届く経路は
    `_notify_managers_slack_dm()`側。
    """
    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=10)
    except Exception:
        logger.exception("refresh_all_projects: failed to post alert to slack")


def _notify_managers_slack_dm(message: str) -> None:
    """`User.isManager = true`の全ユーザーへSlack DMで通知する
    (`src/notifications/manager_dm.py`、2026-08-25新設)。`SLACK_WEBHOOK_URL_ALERT`が本番
    未設定と判明している中で唯一本番で実際に届く通知経路であるため、`src/sync_engine/
    slack_notifier.py`の`WebhookSlackNotifier._notify_managers()`と同じ理由でこちらを主経路と
    する。`manager_dm`はここで遅延importする(`WebhookSlackNotifier._notify_managers()`の
    docstring参照。循環import回避が主目的だが、project_mirror/syncからの参照でも同じ慣習に
    揃える)。`manager_dm.notify_managers()`自体が例外を握りつぶす設計だが、念のためここでも
    捕捉し、Slack通知の失敗でsweep中止の判断自体を失敗させない。
    """
    from src.notifications import manager_dm

    try:
        manager_dm.notify_managers(message, log_context="refresh_all_projects")
    except Exception:
        logger.exception("refresh_all_projects: failed to notify managers via Slack DM")
