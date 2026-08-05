"""コンフリクト判定・解決ロジック（05_同期・競合制御【コンフリクト判定フロー】）。

あるプロパティについて、各ツールでの現在値とupdated_atを受け取り、フローチャートの
3分岐（一致/片方空欄/双方異なる値）を純粋関数として実装する。実際の書き込み
（各SyncTargetへの反映、スプレッドシート「同期ログ」タブへの追記、Slack通知の送信）は
呼び出し側（dispatcher）の責務とし、ここでは「何をすべきか」を表す結果型を組み立てる
ところまでを担う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.db_schema.base import Tool

# src/sync_engine/conflict_resolver.py から見て、リポジトリルート/config/ を指す。
DEFAULT_ALERT_PROPERTIES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "conflict_alert_properties.json"
)


class ResolutionAction(str, Enum):
    """コンフリクト判定フローの最終的な分岐結果。"""

    NO_OP = "no_op"  # 1. 各ツールの値が一致 → 処理なし
    PROPAGATE_VALUE = "propagate_value"  # 2-YES・値を追記した側が新しい → 他方へ値を補完
    PROPAGATE_DELETE = "propagate_delete"  # 2-YES・空欄化した側が新しい → 他方も空欄化
    NOTION_OVERRIDE = "notion_override"  # 2-NO（双方に異なる値） → Notionの値で他方を上書き


@dataclass(frozen=True)
class ToolValue:
    """あるプロパティについて、あるツール上での現在値とその最終更新日時。"""

    tool: Tool
    value: Any
    updated_at: datetime


@dataclass(frozen=True)
class RejectedData:
    """05_同期・競合制御「データ退避」。

    双方に異なる値が存在しNotion優先で上書きした際、消失させてはいけない却下データを表す。
    スプレッドシート「同期ログ」タブへの実際の書き込みはspreadsheet_syncの責務であり、
    ここでは退避すべきデータを組み立てるところまでを担う。
    """

    record_id: str  # 対象ID（Notion主キー）
    property_name: str  # 項目名
    adopted_value: Any  # 採用値（Notionの値）
    rejected_value: Any  # 却下値
    rejected_tool: Tool  # 却下された値を保持していたツール
    occurred_at: datetime  # 発生日時（コンフリクト検知時刻）


@dataclass(frozen=True)
class ConflictResolution:
    """コンフリクト判定の結果。resolve_conflictの戻り値。"""

    action: ResolutionAction
    record_id: str
    property_name: str
    resolved_value: Any = None
    # 反映が必要な（現在値がresolved_valueと異なる）ツールの集合。NOTION_OVERRIDE時は
    # 送信元ツールであっても却下対象なら含まれる（Self-Exclusionは通常propagation時の
    # ルールであり、既に競合が発生した後の是正書き込みには適用しない）。
    target_tools: frozenset[Tool] = field(default_factory=frozenset)
    rejected: tuple[RejectedData, ...] = ()
    notify_slack: bool = False


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _values_equal(a: Any, b: Any) -> bool:
    return a == b


def load_important_properties(
    path: Path | None = None,
) -> dict[str, frozenset[str]]:
    """05_同期・競合制御「アラート通知」対象の重要項目リストを設定ファイルから読み込む。

    設定ファイルが存在しない場合は空辞書を返す（＝重要項目なし＝Slack通知は発生しない）。
    """
    target_path = path or DEFAULT_ALERT_PROPERTIES_PATH
    if not target_path.exists():
        return {}
    raw = json.loads(target_path.read_text(encoding="utf-8"))
    return {
        db_key: frozenset(names)
        for db_key, names in raw.items()
        if db_key != "_comment"
    }


def is_important_property(
    db_key: str,
    property_name: str,
    important_properties: Mapping[str, frozenset[str]] | None = None,
) -> bool:
    """このプロパティが「重要項目」（自動解決時にSlack通知が必要）かどうかを判定する。"""
    config = important_properties if important_properties is not None else load_important_properties()
    return property_name in config.get(db_key, frozenset())


def resolve_conflict(
    record_id: str,
    property_name: str,
    candidates: Sequence[ToolValue],
    *,
    db_key: str,
    detected_at: datetime,
    important_properties: Mapping[str, frozenset[str]] | None = None,
) -> ConflictResolution:
    """【コンフリクト判定フロー】をそのまま実装する。

    candidates: このプロパティについて、現時点で値を保持している各ツールの現在値・更新日時。
    Notion（マスターDB）は常にこのプロパティの保持元であるため、必ず1件はTool.NOTIONの
    エントリを含めること（含まれない場合はValueError）。

    純粋関数として呼び出し側からの副作用（現在時刻取得・DB問い合わせ・通知送信）を排除するため、
    detected_at（コンフリクト検知時刻）は呼び出し側から明示的に渡す。
    """
    if not candidates:
        raise ValueError("candidates must not be empty")
    notion_candidates = [c for c in candidates if c.tool is Tool.NOTION]
    if not notion_candidates:
        raise ValueError("candidates must include a Tool.NOTION entry (Notion is always the master)")
    notion_value = notion_candidates[0].value

    # 1. 各ツールの値が一致しているか？
    first_value = candidates[0].value
    if all(_values_equal(c.value, first_value) for c in candidates):
        return ConflictResolution(action=ResolutionAction.NO_OP, record_id=record_id, property_name=property_name)

    nonempty = [c for c in candidates if not _is_empty(c.value)]
    distinct_nonempty_values: list[Any] = []
    for c in nonempty:
        if not any(_values_equal(c.value, v) for v in distinct_nonempty_values):
            distinct_nonempty_values.append(c.value)

    has_empty = any(_is_empty(c.value) for c in candidates)

    # 2. 一方が「空欄（NULL）」か？（＝非空の値が単一種類のみで、かつ空欄が存在する）
    if has_empty and len(distinct_nonempty_values) <= 1:
        # BLOCKER1対応：updated_atが同時刻でタイした場合、max()は候補リストの先頭要素を
        # 返すため、たまたま空欄側が先頭にあると「空欄化が新しい」と誤判定されデータが
        # 消失する事故につながる（例: Notionレコードが取得できずnotion_current=Noneかつ
        # notion_updated_at=event.occurred_atにフォールバックし、ソース側の新しい値と
        # 同時刻でタイするケース）。同時刻タイでは「値あり」の候補を優先する。
        latest_time = max(c.updated_at for c in candidates)
        latest_candidates = [c for c in candidates if c.updated_at == latest_time]
        nonempty_latest = [c for c in latest_candidates if not _is_empty(c.value)]
        latest = nonempty_latest[0] if nonempty_latest else latest_candidates[0]
        if _is_empty(latest.value):
            resolved_value = None
            action = ResolutionAction.PROPAGATE_DELETE
        else:
            resolved_value = latest.value
            action = ResolutionAction.PROPAGATE_VALUE
        # 空欄化（resolved_value=None）の場合、候補側の空欄が "" とNoneのどちらで表現されて
        # いても「既に空欄」であれば書き込み不要とみなす（_values_equalの厳密比較だと
        # ""とNoneが不一致になり、既に空のツールへ無駄な上書きが発生してしまうため）。
        target_tools = frozenset(
            c.tool
            for c in candidates
            if (not _is_empty(c.value) if resolved_value is None else not _values_equal(c.value, resolved_value))
        )
        return ConflictResolution(
            action=action,
            record_id=record_id,
            property_name=property_name,
            resolved_value=resolved_value,
            target_tools=target_tools,
        )

    # 2-NO: 双方に異なる値が存在する（データ競合） → Notion（マスターDB）の値を正とする。
    target_tools = frozenset(
        c.tool for c in candidates if not _values_equal(c.value, notion_value)
    )
    rejected = tuple(
        RejectedData(
            record_id=record_id,
            property_name=property_name,
            adopted_value=notion_value,
            rejected_value=c.value,
            rejected_tool=c.tool,
            occurred_at=detected_at,
        )
        for c in nonempty
        if c.tool is not Tool.NOTION and not _values_equal(c.value, notion_value)
    )
    notify_slack = bool(rejected) and is_important_property(db_key, property_name, important_properties)

    return ConflictResolution(
        action=ResolutionAction.NOTION_OVERRIDE,
        record_id=record_id,
        property_name=property_name,
        resolved_value=notion_value,
        target_tools=target_tools,
        rejected=rejected,
        notify_slack=notify_slack,
    )
