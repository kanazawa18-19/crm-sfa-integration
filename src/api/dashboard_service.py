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

from src.analytics.forecast import ForecastProject, forecast_quarter
from src.analytics.member_performance import (
    MemberActionRecord,
    MemberProjectRecord,
    compute_member_performance,
)
from src.api.action_classifier import classify_action_type
from src.api.notion_display import page_to_display_dict
from src.api.user_directory import NotionUserDirectory
from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA, classify_status
from src.reports.daily_report import (
    DailyActionRecord,
    DailyProjectRecord,
    build_daily_report_data,
)
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

_CACHE_TTL_ENV_VAR = "DASHBOARD_CACHE_TTL_SECONDS"
_DEFAULT_CACHE_TTL_SECONDS = 60.0

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
            PROJECT_SCHEMA.key, PROJECT_SCHEMA.notion_database_id
        )
        self._action_client = action_client or HttpNotionClient(
            ACTION_SCHEMA.key, ACTION_SCHEMA.notion_database_id
        )
        self._user_directory = user_directory or _cached(
            "user_directory", NotionUserDirectory
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


def build_dashboard_summary(
    as_of: date | None = None,
    *,
    data_source: NotionDataSource | None = None,
) -> dict[str, Any]:
    """全案件のクオーター着地予測・ステータス内訳・件数集計をまとめて返す。

    as_ofは現状フォレキャスト自体の算出には使用しない（forecast_quarterは案件の現時点の
    スナップショットのみを対象とする純粋関数のため）。将来、期間を絞った集計が必要に
    なった場合の拡張余地として引数だけ残している。

    未知の営業ステータス値（classify_statusがValueErrorを送出する値）を持つ案件は、
    ログにwarningを出した上でステータス内訳・件数集計から除外する
    （forecast_quarter側はACTIVE_STATUSES等に含まれない値を元々無視するため対応不要）。
    """
    resolved_as_of = as_of or _today_jst()
    source = data_source or NotionDataSource()
    projects = source.get_projects()

    forecast_projects = [
        ForecastProject(
            project_id=p["notion_page_id"],
            confidence=p.get(PROP_確度),
            status=p.get(PROP_営業ステータス),
            initial_fee=p.get(PROP_初期費用) or 0.0,
            monthly_fee=p.get(PROP_月額費用) or 0.0,
        )
        for p in projects
    ]
    forecast = forecast_quarter(forecast_projects)

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
            "max": {"initial_fee": forecast.max.initial_fee, "mrr": forecast.max.mrr},
            "expected": {
                "initial_fee": forecast.expected.initial_fee,
                "mrr": forecast.expected.mrr,
            },
            "min": {"initial_fee": forecast.min.initial_fee, "mrr": forecast.min.mrr},
        },
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
