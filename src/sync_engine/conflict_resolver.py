"""コンフリクト判定・解決ロジック（05_同期・競合制御【コンフリクト判定フロー】）。

あるプロパティについて、各ツールでの現在値とupdated_atを受け取り、フローチャートの
分岐（1. 各ツールの値が一致 / 2. 一致しない）を純粋関数として実装する。
「一致しない」場合（片方が空欄になったケース・双方に異なる値が存在するケースの
いずれも含む）は、「最終更新日時（updated_at）が最も新しい候補を採用する」という
単一のルールで統一的に解決する（Notionが常に優先されるわけではない。複数候補の
updated_atが完全に同時刻でタイした場合のみ、フォールバックとしてNotionを優先する）。
実際の書き込み（各SyncTargetへの反映、スプレッドシート「同期ログ」タブへの追記、
Slack通知の送信）は呼び出し側（dispatcher）の責務とし、ここでは「何をすべきか」を
表す結果型を組み立てるところまでを担う。
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
    """コンフリクト判定フローの最終的な分岐結果。

    値が食い違う場合（片方が空欄になったケース・双方に異なる値が存在するケースの
    いずれも）は「最終更新日時が最も新しい候補を採用する」という単一のルールで解決する。
    Notionが常に勝つわけではない（同時刻タイの場合のみNotionをフォールバックとして
    優先する）。
    """

    NO_OP = "no_op"  # 1. 各ツールの値が一致 → 処理なし
    PROPAGATE_VALUE = "propagate_value"  # 最新の更新が「値の追加/変更」だった → 他方へ値を伝播
    PROPAGATE_DELETE = "propagate_delete"  # 最新の更新が「空欄化」だった → 他方も空欄化
    NOTION_OVERRIDE = "notion_override"  # 最新の更新がNotion側だった → Notionの値を他方へ伝播


@dataclass(frozen=True)
class ToolValue:
    """あるプロパティについて、あるツール上での現在値とその最終更新日時。"""

    tool: Tool
    value: Any
    updated_at: datetime


@dataclass(frozen=True)
class RejectedData:
    """05_同期・競合制御「データ退避」。

    非空の値が2種類以上存在するデータ競合が発生し、最終更新日時が最も新しい候補の値を
    採用した際に、消失させてはいけない却下データ（採用されなかった側の値）を表す。
    採用側が常にNotionとは限らない（最新編集優先ルールのため、Notion自身の値が
    却下される側になるケースもある）ため、adopted_toolフィールドで「実際にどのツールの
    値が採用されたか」を明示する（却下側の情報だけでは通知・ログの読み手が
    「では誰の編集が勝ったのか」を判別できないため）。スプレッドシート「同期ログ」タブへの
    実際の書き込みはspreadsheet_syncの責務であり、ここでは退避すべきデータを組み立てる
    ところまでを担う。
    """

    record_id: str  # 対象ID（Notion主キー）
    property_name: str  # 項目名
    adopted_value: Any  # 採用値（最終更新日時が最も新しかった候補の値。Notionとは限らない）
    adopted_tool: Tool  # 採用された値を保持していたツール（＝実際に競合に勝ったツール。Notionとは限らない）
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
    # 反映が必要な（現在値がresolved_valueと異なる）ツールの集合。採用されなかった側は
    # 送信元ツールであっても却下対象なら含まれる（Self-Exclusionは通常propagation時の
    # ルールであり、既に競合が発生した後の是正書き込みには適用しない）。
    target_tools: frozenset[Tool] = field(default_factory=frozenset)
    rejected: tuple[RejectedData, ...] = ()
    notify_slack: bool = False


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _values_equal(a: Any, b: Any) -> bool:
    return a == b


def _pick_tie_break_winner(tied: Sequence[ToolValue]) -> ToolValue:
    """updated_atが同時刻で並んだ候補（`tied`）から1件を決定的に選ぶ。

    WARN1/WARN2対応：以前は単に`tied[0]`（呼び出し側が渡したリストの先頭）を採用しており、
    「Notionが勝つ」のはdispatcher.py側がcandidatesの先頭に常にTool.NOTIONを配置している
    という暗黙の呼び出し規約に依存していた（かつ、non-Notion同士のタイでは、その規約が
    保証しないfrozenset由来の順序に事実上依存し、PYTHONHASHSEEDのプロセス間差異で
    非決定的になり得た）。ここではTool.NOTIONを明示的に優先し（Notionが含まれなければ、
    Tool.value（文字列）の昇順という安定したキーで選ぶことで、呼び出し側がcandidatesを
    どの順序で構築してもリスト順序に依存しない再現可能な結果にする。
    """
    notion_candidate = next((c for c in tied if c.tool is Tool.NOTION), None)
    if notion_candidate is not None:
        return notion_candidate
    return min(tied, key=lambda c: c.tool.value)


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

    # 1. 各ツールの値が一致しているか？
    first_value = candidates[0].value
    if all(_values_equal(c.value, first_value) for c in candidates):
        return ConflictResolution(action=ResolutionAction.NO_OP, record_id=record_id, property_name=property_name)

    nonempty = [c for c in candidates if not _is_empty(c.value)]
    distinct_nonempty_values: list[Any] = []
    for c in nonempty:
        if not any(_values_equal(c.value, v) for v in distinct_nonempty_values):
            distinct_nonempty_values.append(c.value)

    # 2. 値が食い違っている（片方が空欄になったケース・双方に異なる値が存在するケースの
    #    いずれも含む） → 最終更新日時（updated_at）が最も新しい候補を採用する。
    #    以前は「片方が空欄」と「双方に異なる値」を別ルールで扱い、後者は常にNotionの
    #    値を優先していたが、これはNotionが実際には古い値しか持っていない場合でも
    #    他ツール側の新しい編集を強制的に上書きしてしまう実害のあるバグだったため、
    #    2026-08本番障害を受けて単一の「最新編集優先」ルールへ統一した。
    #
    #    BLOCKER1対応：updated_atが同時刻でタイした場合、max()は候補リストの先頭要素を
    #    返すため、たまたま空欄側が先頭にあると「空欄化が新しい」と誤判定されデータが
    #    消失する事故につながる（例: Notionレコードが取得できずnotion_current=Noneかつ
    #    notion_updated_at=event.occurred_atにフォールバックし、ソース側の新しい値と
    #    同時刻でタイするケース）。同時刻タイでは「値あり」の候補を優先する。
    #    さらに、値ありの候補同士が同時刻で完全にタイした場合（双方に異なる値が存在し、
    #    かつupdated_atが一致するケース）は、_pick_tie_break_winner()がNotionを明示的に
    #    優先し（WARN2対応）、Notionが候補に含まれない場合はTool.valueの昇順という
    #    決定的なキーで選ぶ（WARN1対応。呼び出し側のリスト順序やfrozenset由来の
    #    非決定的なイテレーション順には依存しない）。
    latest_time = max(c.updated_at for c in candidates)
    latest_candidates = [c for c in candidates if c.updated_at == latest_time]
    nonempty_latest = [c for c in latest_candidates if not _is_empty(c.value)]
    tied = nonempty_latest if nonempty_latest else latest_candidates
    latest = _pick_tie_break_winner(tied)

    if _is_empty(latest.value):
        resolved_value = None
        action = ResolutionAction.PROPAGATE_DELETE
    elif latest.tool is Tool.NOTION:
        resolved_value = latest.value
        action = ResolutionAction.NOTION_OVERRIDE
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

    # データ退避（RejectedData）・Slackアラート対象は、非空の値が2種類以上存在した
    # （＝実際にデータ競合が発生した）場合のみ。「片方が空欄」で値の追加・空欄化のみが
    # 起きたケース（distinct_nonempty_values<=1）は競合ではないため却下データを作らない。
    # 採用値と異なる非空の値を持っていた候補はすべて却下対象とする。これはNotion自身が
    # 採用されなかった側（＝最新編集が他ツールだった）場合も同様に扱う
    # （このRejectedDataは「Notion優先で上書きした際の退避」ではなく、一般に
    # 「データ競合が発生し解決した際の退避」を表すため）。
    rejected: tuple[RejectedData, ...] = ()
    notify_slack = False
    if len(distinct_nonempty_values) >= 2:
        rejected = tuple(
            RejectedData(
                record_id=record_id,
                property_name=property_name,
                adopted_value=resolved_value,
                adopted_tool=latest.tool,
                rejected_value=c.value,
                rejected_tool=c.tool,
                occurred_at=detected_at,
            )
            for c in nonempty
            if not _values_equal(c.value, resolved_value)
        )
        notify_slack = bool(rejected) and is_important_property(db_key, property_name, important_properties)

    return ConflictResolution(
        action=action,
        record_id=record_id,
        property_name=property_name,
        resolved_value=resolved_value,
        target_tools=target_tools,
        rejected=rejected,
        notify_slack=notify_slack,
    )
