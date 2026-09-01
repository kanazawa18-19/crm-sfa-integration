"""案件管理DB（Notion）→ ProjectMirror（Postgres）への同期処理本体（2026-08-17）。

データの正本は引き続きNotionであり、本モジュールは以下3つのエントリポイントを提供する。

- `sync_project_to_mirror()`: Notion Webhook経由の1件更新用
  （`src/sync_engine/webhook_handlers/notion_webhook.py`の`calendar_sync`/`lead_sync`と同じ
  拡張点パターン）。
- **`refresh_projects_incrementally()`: 夜間reconciliation cronの現役の入口**（2026-09-01〜）。
  時間予算で区切って中断し、しおり（`SyncCursor`）に続きを残す分割実行。
- `refresh_all_projects()`: **ローカルからの初回バックフィル専用**
  （`scripts/backfill_project_mirror.py`）。全件を取り切ってから書くため実行時間の上限が
  無い場所でしか使えない。**夜間cronからはもう呼ばれていない。**

■ なぜ全件版と分割実行版が並んでいるのか（2026-09-01）

案件管理DBは26,017件あり、全件取得だけで数分〜十数分かかる。Vercelの実行上限は300秒
なので、cronからは全件版を使えない。一方ローカル実行には上限が無く、一晩で一気に
埋めたい初回バックフィルでは全件版の方が単純で速い。**用途が違うので両方残している。**
片方だけ直して満足しないよう、安全装置を変えるときは必ず両方を見ること。
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, Mapping, Protocol

import requests

from src.api.notion_display import project_page_to_mirror_record
from src.db_schema.project import PROJECT_SCHEMA
from src.project_mirror.db import (
    get_project_count,
    release_refresh_lock,
    sweep_projects,
    try_acquire_refresh_lock,
    upsert_project,
    upsert_projects,
    upsert_projects_and_sweep,
)
from src.sync_engine.clients._notion_paging import query_keyset_slice
from src.sync_engine.sync_cursor import SyncCursor, clear_cursor, load_cursor, save_cursor
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

    #: 分割実行（`refresh_projects_incrementally`）が使う。Database Queryを1回だけ叩き、
    #: ページングは`src/sync_engine/clients/_notion_paging.py`側が行う。
    def query_raw(self, body: dict[str, Any]) -> Mapping[str, Any]: ...


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
    """案件管理DB全件をミラーへ反映する（**ローカルからの初回バックフィル専用**）。

    **夜間reconciliation cronはこの関数を使っていない**（2026-09-01〜。300秒に収まらない
    ため`refresh_projects_incrementally()`へ切り替えた）。全件を取り切ってから書く作りなので、
    実行時間の上限が無い場所からのみ呼ぶこと。

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
            _notify_slack_alert(message, source="refresh_all_projects")
            _notify_managers_slack_dm(message, source="refresh_all_projects")
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
                _notify_slack_alert(message, source="refresh_all_projects")
                _notify_managers_slack_dm(message, source="refresh_all_projects")
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


#: このしおりの名前（`SyncCursor`テーブルのキー）。進み具合はこの行を見れば分かる。
CURSOR_NAME = "project_mirror"

#: **取得に**使ってよい秒数。Vercelの実行上限は300秒。
#: この予算は`query_keyset_slice()`の取得だけに掛かり、そのあとの行の変換・UPSERT・
#: 件数の数え直し・しおりの保存は予算の外側で走る（2026-09-01、ChatGPTのレビュー指摘）。
#: 300秒で強制終了されるとしおりを保存できず、**翌晩も同じ区間をやり直して一巡が進まない**
#: （書き込み自体は冪等なのでデータは壊れないが、永久に終わらなくなる）。
#: 残りを書き込み側の余裕として空けておく。
#: **`src/relation_sync/sync.py`にも同名の定数がある。片方だけ変えないこと**
#: （Vercelの上限が変わった／1周の粒度を見直す、はどちらも両方に効く話）。
DEFAULT_TIME_BUDGET_SECONDS = 170.0

#: 1周で取る件数。中断の粒度になる（小さいほど時間予算を守りやすい）。
_ROUND_LIMIT = 2_000


def _round_limit() -> int:
    """1周で取る件数。環境変数で上書きできる（2026-09-01、Gemini Proのレビュー指摘）。

    同じ作成日時がこの件数ぶん並ぶと、キーセット方式は前へ進めなくなる（`stalled`）。
    Notion APIにはタイブレーカー（ページIDでの並び替え）が無いので、これは仕様上の限界で、
    **コード側では自力回復できない。** 一括移行でタイムスタンプが固まっているDBに当たると
    一巡が止まるため、**コードを直してデプロイしなくても運用者が突破できる口**を用意する。

    実測（2026-09-01）では、案件管理DB・取引先マスターとも一括移行の山でおよそ250件/分で、
    上限の2,000件までは十分な余裕がある。
    """
    raw = os.environ.get("PROJECT_MIRROR_ROUND_LIMIT", "").strip()
    if not raw:
        return _ROUND_LIMIT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "PROJECT_MIRROR_ROUND_LIMIT=%r は整数として読めません。既定の%d件を使います", raw, _ROUND_LIMIT
        )
        return _ROUND_LIMIT
    if value <= 0:
        logger.warning(
            "PROJECT_MIRROR_ROUND_LIMIT=%d は正の数ではありません。既定の%d件を使います", value, _ROUND_LIMIT
        )
        return _ROUND_LIMIT
    logger.info("PROJECT_MIRROR_ROUND_LIMIT=%d で1周の件数を上書きします（既定は%d件）", value, _ROUND_LIMIT)
    return value


def refresh_projects_incrementally(
    *,
    notion_client: ProjectMirrorNotionClient,
    user_directory: Any,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """案件管理DBを**何回かに分けて**ミラーへ反映する（2026-09-01）。

    ■ なぜ分けるのか

    `refresh_all_projects()`は全件を取り切ってから書き込む作りで、案件管理DBが
    **26,017件**（1万件の壁を越えて数え直した実数）ある今は1回の実行では終わらない。
    Vercelの実行上限は300秒。**「1万件で静かに切れる」を直した結果、今度は
    「時間切れで何もしない」に化ける。** 取引先名インデックス
    （`src/relation_sync/sync.py`の`refresh_client_names_incrementally()`）で先に
    採った形を、こちらにも同じように入れる。

    時間予算で区切って中断し、しおり（`SyncCursor`、キーは`CURSOR_NAME`）に
    「どこまで取ったか」を残す。次の夜の実行が続きから再開し、何晩かで一巡する。
    ローカルから本番DBへは書けない（`DATABASE_URL`がVercel側でSensitive）ため、
    **毎晩のcronが自力で追いつく形にするのが唯一の道**。

    ■ 掃除は一巡し終えたときだけ

    掃除は「今回見なかった行を消す」やり方。途中で呼ぶと**まだ見ていないだけの行を
    消してしまう**（2026-08-25にProjectMirrorを全消失させた事故と同じ形）。
    一巡を始めた時刻（`SyncCursor.pass_started_at`）を覚えておき、一巡し終えたときだけ
    それより古い行を消す。

    ■ `refresh_all_projects()`の2つのガードをどう引き継いだか

    - **中身の完全性**（必須プロパティの充足率、2026-08-26の「行数は正常だが中身が
      丸ごと欠落」事故への対策）は**1回ぶんの取得ごとに**見る。下回ったら
      **書き込まず、しおりも進めない**（次回また同じところを取り直す）。
      安全側に倒す判断で、壊れたデータを書くくらいなら止まって毎晩鳴らす。
    - **件数の急減**（部分取得の疑い）は、1回ぶん（2,000件）と全体（26,017件）を
      比べても意味が無いので、**掃除の直前に一巡ぶんの実績で見る。**
      「この一巡で触れた行数」＝掃除が消し残す行数を数え、全体の半分を下回るなら
      掃除を中止する。中止したときは**しおりを捨てて、次の夜に先頭からやり直す**
      （同じ古い基準時刻で判定し続けても状況は変わらないため）。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_projects_incrementally: 既に別プロセスが実行中と判断したためスキップします"
            "（pg_try_advisory_lockの取得に失敗）"
        )
        return {
            "synced_count": 0,
            "deleted_count": 0,
            "completed": False,
            "skipped": "already_running",
        }
    try:
        cursor = load_cursor(CURSOR_NAME)
        # **運用者が「進んでいるのか止まっているのか」を朝ログで判断できるようにする。**
        # 一巡に何晩もかかる仕組みなので、「新しく始めた」のか「続きから再開した」のかが
        # 分からないと、同じような行が毎晩流れるだけになる（2026-09-01、レビュー指摘）。
        if cursor.is_new_pass:
            logger.info(
                "refresh_projects_incrementally: 新しい一巡を始めます（基準時刻=%s）",
                cursor.pass_started_at,
            )
        else:
            logger.info(
                "refresh_projects_incrementally: 前回の続き（%s 以降）から再開します"
                "（この一巡の基準時刻=%s）",
                cursor.watermark,
                cursor.pass_started_at,
            )

        def _post(body: dict[str, Any]) -> Mapping[str, Any]:
            return notion_client.query_raw(body)

        slice_ = query_keyset_slice(
            _post,
            watermark=cursor.watermark,
            round_limit=_round_limit(),
            time_budget_seconds=time_budget_seconds,
            label=CURSOR_NAME,
        )
        rows = [_page_to_mirror_row(page, user_directory=user_directory) for page in slice_.pages]

        # **取りこぼしたまま掃除へ進ませない**（2026-09-01、Geminiレビュー指摘）。
        # 同じ作成日時が1周の上限ぶん並ぶと、キーセット方式は前へ進めなくなる。
        # 以前はそこで completed=True を返しており、呼び出し元は「取り切った」と誤認して
        # 掃除に進み、**まだ見ていない後半の行が全部消える**経路になっていた。
        # 取得済みが全体の半分を超えていれば件数の急減チェックもすり抜ける。
        # 自力では回復しない（round_limitを上げるか、データ側の重なりを解消するしかない）ので、
        # 取れた分だけ反映して、しおりはそのまま残し、人へ届ける。
        if slice_.stalled:
            upsert_projects(rows, synced_at=cursor.pass_started_at)
            save_cursor(dataclasses.replace(cursor, watermark=slice_.watermark))
            message = (
                f"refresh_projects_incrementally: 同じ作成日時が1周の上限({_round_limit()}件)ぶん並んでおり"
                f"これ以上進めません（created_time={slice_.watermark}）。**取りこぼしています。**"
                "掃除は行っていません（既存データは消していません）。"
                "環境変数 PROJECT_MIRROR_ROUND_LIMIT を上げるか、作成日時の重なりを解消してください。"
                "直るまで一巡は完了しません。"
            )
            logger.error(message)
            _notify_slack_alert(message, source="refresh_projects_incrementally")
            _notify_managers_slack_dm(message, source="refresh_projects_incrementally")
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "completed": False,
                "skipped": "keyset_stalled",
            }


        # 「行数は正常だが中身(必須プロパティ)が壊れている」事故の検知(2026-08-26)。
        # 分割実行では1回ぶんの取得に対して掛ける。下回ったら書かず、しおりも進めない。
        # 「行数は正常だが中身(必須プロパティ)が壊れている」事故の検知(2026-08-26)。
        # **小さいスライスも素通りさせない**（2026-09-01、Gemini・ChatGPTが独立に指摘）。
        # 分割実行では一巡の最後に20件未満のスライスが正常に出るため、
        # 件数の下限だけで判定すると、そこが検査なしの穴になる。
        # ただし数件しかない標本に9割の閾値を掛けると、たまたま1件空欄なだけで
        # 止まってしまう（しおりを進めないので一巡が永久に終わらない）。
        # そこで**小さいスライスでは「全件が欠落」のときだけ**止める。
        if rows:
            fill_ratios = _required_property_fill_ratios(rows)
            threshold = (
                _MIN_REQUIRED_PROPERTY_RATIO
                if len(rows) >= _MIN_ROWS_FOR_COMPLETENESS_CHECK
                else 0.0  # 0.0を下回る比率は無いので、実質「全件欠落」だけが該当する
            )
            insufficient = {
                name: ratio
                for name, ratio in fill_ratios.items()
                if ratio < threshold or (threshold == 0.0 and ratio == 0.0)
            }
            if insufficient:
                message = (
                    f"refresh_projects_incrementally: 今回取得した{len(rows)}件のうち必須"
                    f"プロパティの充足率が閾値({_MIN_REQUIRED_PROPERTY_RATIO:.0%})を"
                    f"下回るものがあり（{insufficient}）、中身が壊れている疑いがあるため"
                    "書き込みを中止しました（既存データは変更しておらず、しおりも進めて"
                    "いないので次回また同じところから取り直します）。"
                )
                logger.error(message)
                _notify_slack_alert(message, source="refresh_projects_incrementally")
                _notify_managers_slack_dm(message, source="refresh_projects_incrementally")
                return {
                    "synced_count": 0,
                    "deleted_count": 0,
                    "completed": False,
                    "skipped": "insufficient_required_properties",
                    "required_property_fill_ratios": fill_ratios,
                }

        upsert_projects(rows, synced_at=cursor.pass_started_at)

        if not slice_.completed:
            save_cursor(dataclasses.replace(cursor, watermark=slice_.watermark))
            logger.info(
                "refresh_projects_incrementally: 途中まで取り込みました"
                "（今回%d件 / この一巡でまだ触れていない行が%d件。次回 %s 以降から続けます）",
                len(rows),
                get_project_count(stale_before=cursor.pass_started_at),
                slice_.watermark,
            )
            return {"synced_count": len(rows), "deleted_count": 0, "completed": False}

        # ここから先は一巡し終えたときだけ通る。掃除の前に急減を確かめる。
        total_count = get_project_count()
        # **掃除が実際に消す行数を直接数える**（2026-09-01）。
        # 「この一巡が触れた行数」の定義でChatGPTとGeminiの指摘が真っ二つに割れた
        # （`>=`だとWebhook更新が混ざって検知が鈍る／等号だと生きている行を数え落として
        # 誤検知する）。生存の数え方を議論せずに済むよう、破壊的操作が消す行数で判断する。
        # Webhookが更新した行は`syncedAt`が基準時刻より未来なので、そもそも消えない。
        stale_count = get_project_count(stale_before=cursor.pass_started_at)
        # **本当に大量削除されたときに、掃除が永久に発火しなくなるのを避ける**
        # （2026-09-01、Geminiレビュー指摘）。Notion側で正当に半分以上が消されると、
        # このチェックが毎回成立して掃除が飛ばされ、消えたはずの行が残るので
        # `total_count`は大きいまま。翌晩も翌々晩も同じ判定になり、**二度と掃除されない。**
        # 部分取得と正当な大量削除は件数だけでは見分けられないので、
        # 運用者が1回だけ許可できる逃げ道を用意する（承認したら環境変数を戻すこと）。
        allow_shrink = os.environ.get("PROJECT_MIRROR_ALLOW_SHRINK", "").strip().lower() == "true"
        # 件数が少ないときの誤検知を避けて閾値の判定は20件以上に限るが、
        # **「1行も触れずに全部消える」だけは件数によらず必ず止める**
        # （2026-08-25の全消失事故そのものの形なので、小さいテーブルでも通さない）。
        would_delete_everything = total_count > 0 and stale_count == total_count
        if not allow_shrink and (
            would_delete_everything
            or (total_count >= 20 and stale_count > total_count * (1 - _MIN_SYNC_RATIO))
        ):
            message = (
                f"refresh_projects_incrementally: 掃除で消える件数({stale_count}件)が"
                f"既存ミラー件数({total_count}件)の半分を超えるため、部分取得の疑いがあり"
                "掃除を中止しました（既存データは変更していません。しおりを捨てたので"
                "次回は先頭から取り直します）。**Notion側で正当に大量削除した結果であれば、"
                f"環境変数 PROJECT_MIRROR_ALLOW_SHRINK=true を設定して1回だけ掃除を通し、"
                "その後は必ず設定を戻してください。**"
            )
            logger.error(message)
            _notify_slack_alert(message, source="refresh_projects_incrementally")
            _notify_managers_slack_dm(message, source="refresh_projects_incrementally")
            clear_cursor(CURSOR_NAME)
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "skipped": "suspected_partial_fetch",
                "completed": True,
            }

        deleted_count = sweep_projects(before=cursor.pass_started_at)
        clear_cursor(CURSOR_NAME)
        logger.info(
            "refresh_projects_incrementally: 一巡し終えました（今回%d件 / 掃除%d件）",
            len(rows),
            deleted_count,
        )
        return {"synced_count": len(rows), "deleted_count": deleted_count, "completed": True}
    finally:
        release_refresh_lock(lock_conn)


def _notify_slack_alert(message: str, *, source: str = "project_mirror") -> None:
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
        logger.exception("%s: failed to post alert to slack", source)


def _notify_managers_slack_dm(message: str, *, source: str = "project_mirror") -> None:
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
        manager_dm.notify_managers(message, log_context=source)
    except Exception:
        logger.exception("%s: failed to notify managers via Slack DM", source)
