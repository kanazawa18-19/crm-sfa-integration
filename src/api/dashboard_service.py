"""ダッシュボード用のオーケストレーション層。

Notion（案件管理DB・アクション履歴DB）から取得した生データを`src/analytics/`・
`src/reports/`の分析関数の入力形式へ変換し、呼び出した結果をJSON変換可能なdictへ
整形する。Notion API呼び出しは`NotionDataSource`に閉じ込め、各`build_*`関数は
`data_source`引数でテスト用のフェイク実装に差し替えられるようにしている。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from src.analytics.fiscal_calendar import (
    fiscal_half_range,
    fiscal_quarter_range,
    fiscal_year_range,
)
from src.analytics.forecast import ForecastAmount, ForecastProject, forecast_quarter
from src.analytics.member_performance import (
    MemberActionRecord,
    MemberProjectRecord,
    compute_member_performance,
)
from src.api.action_classifier import classify_action_type
from src.api.notion_display import page_to_display_dict
from src.api.user_directory import NotionUserDirectory
from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.project import (
    ACTIVE_STATUSES,
    CONFIDENCE_LEVELS,
    CONFIRMED_STATUSES,
    PROJECT_SCHEMA,
    classify_status,
)
from src.reports.daily_report import (
    DailyActionRecord,
    DailyProjectRecord,
    build_daily_report_data,
)
from src.sync_engine.clients._http import INTERACTIVE_MAX_RATE_LIMIT_RETRIES
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

_CACHE_TTL_ENV_VAR = "DASHBOARD_CACHE_TTL_SECONDS"
# 案件管理DB全件（get_projects）のコールドフェッチは実測で約100秒かかる
# （2026-08-17、Suspenseストリーミング化タスクのレビューで判明。案件数・Notion API
# レイテンシ次第でさらに伸びる可能性もある）。デフォルトTTLがそれより短いと、
# 全社ダッシュボードのように60秒に1回以上のペースでアクセスされる画面では
# キャッシュがほぼ常に無効化され、実質毎回コールドフェッチ相当の遅さになってしまう。
# 全社ダッシュボードの用途上10分程度のデータ鮮度低下は許容範囲と判断し600秒に引き上げた
# （webhookベースのリアルタイム同期とは別物であり、この値を変えても同期パイプラインの
# 正確性には影響しない）。
_DEFAULT_CACHE_TTL_SECONDS = 600.0

_STALLED_DAYS_ENV_VAR = "MANAGER_ALERT_STALLED_DAYS"
_DEFAULT_STALLED_DAYS = 14

# `NotionDataSource`がデフォルト引数で生成される経路（build_*関数がdata_source未指定で
# 呼ばれた場合）にのみ効くプロセス内・TTLベースの簡易キャッシュ。`data_source`を明示的に
# 注入するテスト（FakeDataSource等）はこのキャッシュを一切経由しない。単一プロセスの
# 簡易デプロイ想定のためロックは持たない（同時リクエストで二重取得が起きても致命的では
# ないため許容する）。
_module_cache: dict[str, tuple[float, Any]] = {}


def _cache_ttl_seconds() -> float:
    raw = os.environ.get(_CACHE_TTL_ENV_VAR)
    if not raw:
        return _DEFAULT_CACHE_TTL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_CACHE_TTL_SECONDS


def _cached(key: str, fetch: Callable[[], Any]) -> Any:
    now = time.monotonic()
    cached = _module_cache.get(key)
    if cached is not None and now - cached[0] < _cache_ttl_seconds():
        return cached[1]
    value = fetch()
    _module_cache[key] = (now, value)
    return value


def reset_cache() -> None:
    """モジュールレベルキャッシュを明示的にクリアする（テスト用）。"""
    _module_cache.clear()


def _today_jst() -> date:
    return datetime.now(_JST).date()

# 案件管理DBのプロパティ名（src/db_schema/project.pyの実データに準拠）。
PROP_案件名 = "案件名"
PROP_営業ステータス = "営業ステータス"
PROP_確度 = "確度"
PROP_初期費用 = "初期費用"
PROP_月額費用 = "月額費用"
PROP_担当メンバー = "担当メンバー"
PROP_次回アクション日 = "次回アクション日"
PROP_提案サービス = "提案サービス"
PROP_作成日時 = "作成日時"
# 契約済案件は実際の契約日、進行中（未契約）案件は営業担当が入力した予想契約日が入る
# 単一プロパティ（src/reports/batch.pyのPROP_契約日と同じ、案件管理DBに実在するプロパティ名）。
PROP_契約日 = "契約日 / 予想契約日"

# アクション履歴DBのプロパティ名（src/db_schema/action.pyの実データに準拠）。
ACTION_TITLE_PROP = "商談回数・電話回数・メール回数（何回目）"
PROP_アクション日 = "アクション日"
PROP_案件名_ACTION = "案件名"
PROP_担当営業 = "担当営業"


def _resolve_person_name(person: Any, user_directory: Any) -> str | None:
    """`notion_display._parse_people`が返す`{"id":..., "name":...}`形式1件を表示名へ変換する。

    Notion APIのページプロパティレスポンスには通常ユーザーの`name`が直接埋め込まれている
    （インテグレーションに「ユーザー情報の読み取り」権限がある場合）。実データ確認の結果、
    `GET /v1/users`（ワークスペースメンバー一覧、`NotionUserDirectory`が使う）には
    ゲストユーザー等の理由で現れないユーザーが存在することが判明したため、
    ページに埋め込まれた`name`を最優先し、`name`が欠落している場合のみ
    `NotionUserDirectory`によるID解決にフォールバックする。
    """
    if not isinstance(person, dict):
        return None
    name = person.get("name")
    # 空白のみの名前（Notion側の入力揺れ）を「解決済み」と誤判定しないよう.strip()する
    # （Geminiクロスレビューでの指摘を反映）。
    if isinstance(name, str) and name.strip():
        return name.strip()
    person_id = person.get("id")
    if not person_id:
        return None
    resolved = user_directory.resolve(str(person_id))
    if resolved == str(person_id):
        # NotionUserDirectory（GET /v1/usersのワークスペースメンバー一覧）でも解決できな
        # かった（削除済みユーザー・ゲスト等、Notion側がそもそも名前情報を返さないケースが
        # 実データで確認されている）。生のUUIDをそのまま表示すると分かりにくいため、
        # 人間が読める形のプレースホルダーに変換する。
        return f"不明なメンバー（{str(person_id)[:8]}）"
    return resolved


def _resolve_people_names(value: Any, user_directory: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names = [_resolve_person_name(person, user_directory) for person in value]
    return [name for name in names if name]


def _first_relation_id(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    return None


def _parse_date(value: Any) -> date | None:
    """Notion表示用のdate/datetime文字列（"2026-08-05"や"2026-08-05T09:00:00.000Z"等）
    から日付部分のみを取り出す。"""
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _json_safe(value: Any) -> Any:
    """dataclasses.asdict()の結果に含まれるdate等をJSON変換可能な値へ再帰的に変換する。"""
    if isinstance(value, dict):
        return {key: _json_safe(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


class NotionDataSource:
    """案件管理DB・アクション履歴DBのNotionページを表示用dictへ変換して取得するデータソース。

    `project_client`/`action_client`（`query_all_pages()`を持つオブジェクト）・
    `user_directory`（`resolve`/`resolve_many`を持つオブジェクト）を注入できる
    （未指定時は実際のNotion APIを叩く`HttpNotionClient`/`NotionUserDirectory`を使う）。
    テストではこれらをフェイク実装に差し替える。
    """

    def __init__(
        self,
        *,
        project_client: Any | None = None,
        action_client: Any | None = None,
        user_directory: Any | None = None,
    ) -> None:
        self._project_client = project_client or HttpNotionClient(
            PROJECT_SCHEMA.key,
            PROJECT_SCHEMA.notion_database_id,
            max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        )
        self._action_client = action_client or HttpNotionClient(
            ACTION_SCHEMA.key,
            ACTION_SCHEMA.notion_database_id,
            max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        )
        self._user_directory = user_directory or _cached(
            "user_directory",
            lambda: NotionUserDirectory(
                max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES
            ),
        )

    def get_projects(self) -> list[dict[str, Any]]:
        return _cached("projects", self._fetch_projects)

    def _fetch_projects(self) -> list[dict[str, Any]]:
        pages = self._project_client.query_all_pages()
        records: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        for page in pages:
            record, skipped = page_to_display_dict(page, PROJECT_SCHEMA)
            records.append(record)
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "get_projects: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                PROJECT_SCHEMA.key,
                sorted(skipped_properties),
            )
        for record in records:
            record[PROP_担当メンバー] = _resolve_people_names(
                record.get(PROP_担当メンバー), self._user_directory
            )
        return records

    def get_actions(self) -> list[dict[str, Any]]:
        return _cached("actions", self._fetch_actions)

    def _fetch_actions(self) -> list[dict[str, Any]]:
        pages = self._action_client.query_all_pages()
        records: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        for page in pages:
            record, skipped = page_to_display_dict(page, ACTION_SCHEMA)
            records.append(record)
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "get_actions: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                ACTION_SCHEMA.key,
                sorted(skipped_properties),
            )
        for record in records:
            record[PROP_担当営業] = self._resolve_assignee(record.get(PROP_担当営業))
        return records

    def _resolve_assignee(self, value: Any) -> str | None:
        """`担当営業`はrollupのため、実データでは`[[{"id":..., "name":...}]]`（rollup配列の中に
        `_parse_people`のpeopleリストがネストされた形）で入ってくる。中身がtext（文字列）の
        rollup構成であるケースにも引き続き対応する防御的実装とする。
        """
        first = (value[0] if value else None) if isinstance(value, list) else value
        if isinstance(first, list):
            first = first[0] if first else None
        if isinstance(first, dict):
            return _resolve_person_name(first, self._user_directory)
        if not first:
            return None
        return self._user_directory.resolve(str(first))


def _forecast_amount_dict(amount: ForecastAmount) -> dict[str, float]:
    return {"initial_fee": amount.initial_fee, "mrr": amount.mrr}


def _period_scoped_forecast_projects(
    projects: list[dict[str, Any]], *, start: date, end: date
) -> list[ForecastProject]:
    """指定期間（start〜end、両端含む）に属する契約済・進行中案件のみを`ForecastProject`へ
    変換する。日付は「契約日 / 予想契約日」（`PROP_契約日`）の1つのプロパティを、契約済案件は
    実際の契約日として、進行中案件は営業担当が入力した予想契約日として読む
    （build_dashboard_summaryのdocstring参照）。この日付が未設定の進行中案件・契約済案件は
    どの期間にも含めない（それぞれunscheduled_active_count/unscheduled_confirmed_countとして
    別途集計する）。失注・解約案件はforecast_quarter()側でも計上対象外のため、そもそも
    この一覧に含めない。
    """
    result = []
    for p in projects:
        status = p.get(PROP_営業ステータス)
        if status not in CONFIRMED_STATUSES and status not in ACTIVE_STATUSES:
            continue
        scoped_date = _parse_date(p.get(PROP_契約日))
        if scoped_date is None or not (start <= scoped_date <= end):
            continue
        result.append(
            ForecastProject(
                project_id=p["notion_page_id"],
                confidence=p.get(PROP_確度),
                status=status,
                initial_fee=p.get(PROP_初期費用) or 0.0,
                monthly_fee=p.get(PROP_月額費用) or 0.0,
            )
        )
    return result


def _period_forecast_dict(
    projects: list[dict[str, Any]], *, start: date, end: date
) -> dict[str, Any]:
    period_projects = _period_scoped_forecast_projects(projects, start=start, end=end)
    forecast = forecast_quarter(period_projects)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "max": _forecast_amount_dict(forecast.max),
        "expected": _forecast_amount_dict(forecast.expected),
        "min": _forecast_amount_dict(forecast.min),
    }


def build_dashboard_summary(
    as_of: date | None = None,
    *,
    data_source: NotionDataSource | None = None,
) -> dict[str, Any]:
    """クオーター/半期/通期の3期間ごとに着地予測・ステータス内訳・件数集計をまとめて返す。

    3期間（クオーター/半期/通期）はいずれもas_ofを含む自社の会計年度（期初12月・期末11月、
    `src.analytics.fiscal_calendar`参照）に基づいて算出する。各期間への案件の帰属判定は
    以下のルール（金沢さん確認済み）に従う。
    - 契約済（CONFIRMED_STATUSES）案件は「契約日 / 予想契約日」（PROP_契約日）の実際の
      契約日が期間内であれば計上する。
    - 進行中（ACTIVE_STATUSES）案件は同じPROP_契約日に営業担当が入力した予想契約日が
      期間内であれば計上する。
    - 進行中案件でPROP_契約日が未設定の場合は、3期間いずれの着地予測にも一切計上しない
      （曖昧に含めたり除外したりせず、サイレントに数字が消えることを防ぐため
      `unscheduled_active_count`として別途件数のみ返す）。
    - 契約済案件でPROP_契約日が未設定の場合も同様に3期間いずれの着地予測にも一切計上しない
      （PROP_契約日はRequirementLevel.OPTIONALのため実際に起こり得るデータ不備であり、
      実績値がサイレントに消えることを防ぐため`unscheduled_confirmed_count`として
      別途件数のみ返す）。

    未知の営業ステータス値（classify_statusがValueErrorを送出する値）を持つ案件は、
    ログにwarningを出した上でステータス内訳・件数集計から除外する
    （forecast_quarter側はACTIVE_STATUSES等に含まれない値を元々無視するため対応不要）。

    上記の注意点は`notes`（トップレベル、forecastの兄弟）に人間可読な日本語の注記文として
    格納し、フロントエンド側でそのまま表示する（build_daily_report等、他のbuild_*関数と
    同じくビジネスロジックをバックエンドに閉じ込める方針に合わせている）。
    """
    resolved_as_of = as_of or _today_jst()
    source = data_source or NotionDataSource()
    projects = source.get_projects()

    quarter_start, quarter_end = fiscal_quarter_range(resolved_as_of)
    half_start, half_end = fiscal_half_range(resolved_as_of)
    year_start, year_end = fiscal_year_range(resolved_as_of)

    unscheduled_active_count = sum(
        1
        for p in projects
        if p.get(PROP_営業ステータス) in ACTIVE_STATUSES and _parse_date(p.get(PROP_契約日)) is None
    )
    unscheduled_confirmed_count = sum(
        1
        for p in projects
        if p.get(PROP_営業ステータス) in CONFIRMED_STATUSES and _parse_date(p.get(PROP_契約日)) is None
    )

    notes = [
        f"予想契約日が未入力の進行中案件が{unscheduled_active_count}件あり、"
        "上記の着地予測には含まれていません。",
        # obasan-qualityレビューBLOCKER対応（2026-08-14）: Max/Expected/Minは
        # それぞれ独立した基準（営業ステータスの値・確度）で算出するため、大小関係を
        # 保証しない（forecast.pyのキャップ撤廃、モジュールdocstring参照）。この注記が
        # 無いと、Minの方がMaxより大きい表示を見た営業マネージャーが「バグでは」と
        # 誤解しかねない。
        "Max（楽観）・Expected（見込み）・Min（悲観）はそれぞれ別の判定基準で算出して"
        "いるため、Minの方がMaxより大きく表示される等、直感に反する場合があります。",
    ]
    if unscheduled_confirmed_count:
        notes.append(
            f"契約済だが契約日が未入力の案件が{unscheduled_confirmed_count}件あり、"
            "着地予測の実績値に反映されていません。"
        )

    breakdown: dict[str, dict[str, Any]] = {}
    confirmed_count = active_count = lost_count = cancelled_count = 0
    for p in projects:
        status = p.get(PROP_営業ステータス)
        try:
            category = classify_status(status)
        except ValueError:
            logger.warning(
                "build_dashboard_summary: 未知の営業ステータス値を検知しました"
                "（ステータス内訳・件数集計から除外します）: %r",
                status,
            )
            continue

        entry = breakdown.setdefault(
            status,
            {
                "status": status,
                "category": category,
                "count": 0,
                "initial_fee_sum": 0.0,
                "monthly_fee_sum": 0.0,
            },
        )
        entry["count"] += 1
        entry["initial_fee_sum"] += p.get(PROP_初期費用) or 0.0
        entry["monthly_fee_sum"] += p.get(PROP_月額費用) or 0.0

        if category == "契約済":
            confirmed_count += 1
        elif category == "進行中":
            active_count += 1
        elif category == "失注":
            lost_count += 1
        elif category == "解約":
            cancelled_count += 1

    return {
        "as_of": resolved_as_of.isoformat(),
        "forecast": {
            "quarter": _period_forecast_dict(projects, start=quarter_start, end=quarter_end),
            "half": _period_forecast_dict(projects, start=half_start, end=half_end),
            "year": _period_forecast_dict(projects, start=year_start, end=year_end),
            "unscheduled_active_count": unscheduled_active_count,
            "unscheduled_confirmed_count": unscheduled_confirmed_count,
        },
        "notes": notes,
        "status_breakdown": list(breakdown.values()),
        "totals": {
            "project_count": confirmed_count + active_count + lost_count + cancelled_count,
            "confirmed_count": confirmed_count,
            "active_count": active_count,
            "lost_count": lost_count,
            "cancelled_count": cancelled_count,
        },
    }


def build_daily_report(
    report_date: date,
    *,
    data_source: NotionDataSource | None = None,
) -> dict[str, Any]:
    """指定日のチーム日報データを組み立てる。

    案件管理DBには変更履歴プロパティが無いため`previous_status`/`status_changed_date`は
    常にNoneのまま渡す（結果的に`status_changes`は常に空配列になる）。
    """
    source = data_source or NotionDataSource()
    projects = source.get_projects()
    actions = source.get_actions()

    action_records = [
        DailyActionRecord(
            project_id=_first_relation_id(a.get(PROP_案件名_ACTION)) or a["notion_page_id"],
            member=a.get(PROP_担当営業) or "未設定",
            action_type=classify_action_type(a.get(ACTION_TITLE_PROP)),
            action_date=action_date,
        )
        for a in actions
        if (action_date := _parse_date(a.get(PROP_アクション日))) is not None
    ]

    project_records = [
        DailyProjectRecord(
            project_id=p["notion_page_id"],
            client_name=p.get(PROP_案件名) or "",
            assignee=(p.get(PROP_担当メンバー) or ["未設定"])[0],
            status=p.get(PROP_営業ステータス),
            created_date=created_date,
            proposed_services=tuple(p.get(PROP_提案サービス) or ()),
            initial_fee=p.get(PROP_初期費用) or 0.0,
            monthly_fee=p.get(PROP_月額費用) or 0.0,
            confidence=p.get(PROP_確度),
            next_action_date=_parse_date(p.get(PROP_次回アクション日)),
        )
        for p in projects
        if p.get(PROP_営業ステータス) is not None
        and (created_date := _parse_date(p.get(PROP_作成日時))) is not None
    ]

    data = build_daily_report_data(
        report_date=report_date, actions=action_records, projects=project_records
    )
    result = _json_safe(asdict(data))
    # dataclasses.asdict()はMemberActionSummary.total（@property）を含まないため、
    # counts_by_typeから明示的に合算して補う。
    for summary, raw_summary in zip(result["member_summaries"], data.member_summaries):
        summary["total"] = raw_summary.total
    result["notes"] = [
        "status_changesは変更履歴データが未整備のため常に空です（docs/dashboard_note.md参照）",
        "action_typeはアクション履歴DBのtitle自由記述からのヒューリスティック推定であり、"
        "正確なアクション種別分類ではありません（詳細はdocs/dashboard_note.md参照）。",
    ]
    return result


def build_member_performance(
    as_of: date,
    *,
    data_source: NotionDataSource | None = None,
) -> dict[str, Any]:
    """メンバー別パフォーマンス（ボリューム・クオリティ・スピード・総合スコア）を算出する。"""
    source = data_source or NotionDataSource()
    projects = source.get_projects()
    actions = source.get_actions()

    member_project_records = [
        MemberProjectRecord(
            project_id=p["notion_page_id"],
            member=member,
            status=p.get(PROP_営業ステータス),
            next_action_date=_parse_date(p.get(PROP_次回アクション日)),
        )
        for p in projects
        if p.get(PROP_営業ステータス) is not None
        for member in (p.get(PROP_担当メンバー) or [])
    ]

    member_action_records = [
        MemberActionRecord(
            project_id=_first_relation_id(a.get(PROP_案件名_ACTION)) or a["notion_page_id"],
            member=a.get(PROP_担当営業) or "未設定",
            action_type=classify_action_type(a.get(ACTION_TITLE_PROP)),
            action_date=action_date,
        )
        for a in actions
        if (action_date := _parse_date(a.get(PROP_アクション日))) is not None
    ]

    performances = compute_member_performance(
        member_project_records, member_action_records, as_of=as_of
    )

    return {
        "as_of": as_of.isoformat(),
        "members": [asdict(m) for m in performances],
        "notes": [
            "action_typeはアクション履歴DBのtitle自由記述からのヒューリスティック推定であり、"
            "正確なアクション種別分類ではありません（詳細はdocs/dashboard_note.md参照）。",
            "期間フィルタは行っておらず、全期間累積の集計です。",
        ],
    }


def _stalled_days_threshold() -> int:
    raw = os.environ.get(_STALLED_DAYS_ENV_VAR)
    if not raw:
        return _DEFAULT_STALLED_DAYS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_STALLED_DAYS


def _manager_alert_entry(
    project: dict[str, Any], *, reason: str, is_proxy: bool = False
) -> dict[str, Any]:
    next_action_date = _parse_date(project.get(PROP_次回アクション日))
    return {
        "notion_page_id": project["notion_page_id"],
        "project_name": project.get(PROP_案件名),
        "assignee": (project.get(PROP_担当メンバー) or ["未設定"])[0],
        "status": project.get(PROP_営業ステータス),
        "confidence": project.get(PROP_確度),
        "next_action_date": next_action_date.isoformat() if next_action_date else None,
        "reason": reason,
        "is_proxy": is_proxy,
    }


def build_manager_alerts(
    as_of: date,
    *,
    data_source: NotionDataSource | None = None,
) -> dict[str, Any]:
    """マネージャー向けアラート（失注・失注候補・停滞・受注）をダッシュボード表示用に集計する。

    ダッシュボード表示専用機能であり、Slack等の外部送信は行わない（別途対応予定）。

    案件管理DBには変更履歴プロパティが無いため、build_daily_reportのstatus_changesと同様に
    「あるタイミングでステータスが変化した」というイベントは検知できない。ここでのアラートは
    すべてリクエスト時点のスナップショット（現在の営業ステータス・確度・次回アクション日）
    から導出したものであり、イベントベースの検知ではない点に注意（notesにも明記する）。

    "lost_candidate"（失注候補）は、classify_status()の区分に「失注候補」に相当する実データ値が
    存在しない（実データ確認済み：「失注」という完全一致の値のみ存在する）ため、
    確度（PROP_確度）が最低ランクの"D"かつ進行中（ACTIVE_STATUSES）の案件を代理指標として
    採用したものであり、実際のステータス値に基づくトリガーではない（notesにも明記する）。

    "stalled"（停滞）は、次回アクション日（PROP_次回アクション日）が未設定、または
    as_ofからstalled_days_threshold日以上前の進行中案件を対象とする、次回アクション日
    ベース（前向き）の指標である。src.analytics.conditionが提供する既存の🔴停滞リスク
    （総接触回数が全社平均を大きく超えても未契約）・🟡要フォロー（最終アクションから
    14日超過、後ろ向き）とは算出根拠が異なる別概念であり、デフォルト閾値が偶然どちらも
    14日である以外に関連はない。両者を混同・マージしない（本関数のstalledは
    「次回アクション日」ベースという製品判断に基づく独立した指標であり、意図的に
    condition.pyのロジックを再利用していない）。

    lost_candidateとstalledは排他ではなく、確度がDかつ次回アクション日が古い/未設定の
    案件は両方のバケットに含まれ得る（notesにも明記する）。
    """
    stalled_days_threshold = _stalled_days_threshold()
    source = data_source or NotionDataSource()
    projects = source.get_projects()

    stalled_cutoff = as_of - timedelta(days=stalled_days_threshold)

    lost: list[dict[str, Any]] = []
    lost_candidate: list[dict[str, Any]] = []
    stalled: list[dict[str, Any]] = []
    won: list[dict[str, Any]] = []

    for p in projects:
        status = p.get(PROP_営業ステータス)
        if status is None:
            continue
        try:
            category = classify_status(status)
        except ValueError:
            logger.warning(
                "build_manager_alerts: 未知の営業ステータス値を検知しました"
                "（アラート集計から除外します）: %r",
                status,
            )
            continue

        if category == "失注":
            lost.append(_manager_alert_entry(p, reason="lost"))
        elif category == "契約済":
            won.append(_manager_alert_entry(p, reason="won"))
        elif category == "進行中":
            if p.get(PROP_確度) == CONFIDENCE_LEVELS[-1]:
                lost_candidate.append(
                    _manager_alert_entry(p, reason="lost_candidate", is_proxy=True)
                )

            next_action_date = _parse_date(p.get(PROP_次回アクション日))
            if next_action_date is None or next_action_date <= stalled_cutoff:
                stalled.append(_manager_alert_entry(p, reason="stalled"))

    alerts = {
        "lost": lost,
        "lost_candidate": lost_candidate,
        "stalled": stalled,
        "won": won,
    }

    return {
        "as_of": as_of.isoformat(),
        "alerts": alerts,
        "counts": {key: len(value) for key, value in alerts.items()},
        "stalled_days_threshold": stalled_days_threshold,
        "notes": [
            "lost_candidate（失注候補）はNotion上に「失注候補」という実データ値が存在しない"
            "ための代理指標です。確度（確度プロパティ）が最低ランクの\"D\"かつ営業ステータスが"
            "進行中区分の案件をリスクが高い案件として暫定的に表示しています。"
            "実際の失注ステータスへの遷移を検知したものではありません。",
            "status_changesと同様、案件管理DBには変更履歴データが未整備のため、本アラートは"
            "全てリクエスト時点のスナップショット判定であり、イベント（ステータス変化）ベースの"
            "検知ではありません（詳細はdocs/dashboard_note.md参照）。",
            f"stalledは次回アクション日が未設定、または{stalled_days_threshold}日以上前の"
            "進行中案件を対象としています（MANAGER_ALERT_STALLED_DAYS環境変数で変更可能）。",
            "stalledは次回アクション日（未来向き）を基準とした独自の指標であり、"
            "週次レポート等で使われるsrc.analytics.conditionの🔴停滞リスク／🟡要フォロー"
            "（最終アクション日・総接触回数ベース）とは算出根拠が異なる別概念です。"
            "デフォルト閾値がどちらも14日なのは偶然の一致であり、両者を同一視しないでください。",
            "lost_candidateとstalledは互いに排他ではありません。確度が\"D\"かつ次回アクション日"
            "が古い/未設定の案件は両方のバケットに重複して含まれる場合があるため、"
            "counts配下の値を単純合算すると案件数を過大にカウントする点に注意してください。",
        ],
    }


_MAX_SEARCH_RESULTS = 20


def search_projects(
    query: str,
    *,
    data_source: NotionDataSource | None = None,
) -> dict[str, Any]:
    """案件名の部分一致（大文字小文字無視）で案件を検索する。

    書類自動生成機能（`src/document_generation/`）でNotionページIDを指定するための
    案件選択UI（`dashboard/`）から呼ばれる想定。案件そのものの詳細集計は行わず、
    選択に必要な最小限の項目のみを返す。
    """
    source = data_source or NotionDataSource()
    projects = source.get_projects()

    normalized_query = query.strip().lower()
    matched = [
        p
        for p in projects
        if normalized_query and normalized_query in (p.get(PROP_案件名) or "").lower()
    ]

    return {
        "projects": [
            {
                "notion_page_id": p["notion_page_id"],
                "project_name": p.get(PROP_案件名) or "",
                "status": p.get(PROP_営業ステータス),
                "proposed_services": p.get(PROP_提案サービス) or [],
            }
            for p in matched[:_MAX_SEARCH_RESULTS]
        ],
        "total_matched": len(matched),
    }
