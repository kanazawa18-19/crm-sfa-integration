"""メンバー別パフォーマンス評価（06_営業分析ロジック /

営業部行動ガイドライン「パフォーマンス ＝ スピード × ボリューム × クオリティ
（マインドセットが全ての土台）」を、案件管理DB・アクション管理DBの既存プロパティのみ
（新規プロパティ追加なし）で近似算出する。

- ボリューム: メンバーごとの期間内総接触回数。`src.analytics.contact_count`の
  COUNTABLE_ACTION_TYPES（自動メール／テレアポ／訪問商談／オンライン商談）を
  メンバー単位に適用する（`count_total_contacts`は案件単位の集計のため転用できない）。
- クオリティ: メンバーごとの受注率。担当案件のうち決着済み（営業ステータスが
  `forecast.CONFIRMED_STATUS`／`LOST_STATUS`／`CANCELLED_STATUS`のいずれか、
  すなわち`forecast.ACTIVE_STATUSES`に含まれない）案件を分母、`CONFIRMED_STATUS`を
  分子とする。
- スピード（簡易代替指標）: 「次回アクション期限遵守率」。本来ガイドラインが求める
  スピード指標（一次返信時間の実測等）に対応するプロパティはNotion側に存在せず、
  新規プロパティ追加は今回のスコープ外のため、既存プロパティのみで算出できる簡易近似
  として採用する。詳細な限界は`_member_deadline_compliance_rates`のdocstringと
  `docs/member_performance_note.md`を参照。

パフォーマンス総合スコアはガイドラインの定義に忠実に3指標の掛け算とする。ボリュームのみ
件数（絶対値）であり他の2指標（0〜1の割合）とそのまま掛け合わせられないため、算出対象
メンバー内の相対値（グループ内最大接触回数に対する比率）で0〜1に正規化した上で掛け算する
（詳細は`compute_member_performance`のdocstring参照）。クオリティ・スピードいずれかが
データ不足でNone（担当案件0件・決着済み案件0件・期限判定対象0件等）の場合、"0点"と
区別するため総合スコアもNoneとする。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from src.analytics.contact_count import COUNTABLE_ACTION_TYPES
from src.analytics.forecast import CANCELLED_STATUS, CONFIRMED_STATUS, LOST_STATUS

# 決着済み（今後動かない）案件を表す営業ステータス。forecast.ACTIVE_STATUSESの補集合
# （契約済・失注・解約の3つ。src/db_schema/project.py「営業ステータス」選択肢参照）。
DECIDED_STATUSES = frozenset({CONFIRMED_STATUS, LOST_STATUS, CANCELLED_STATUS})


@dataclass(frozen=True)
class MemberProjectRecord:
    """案件管理DBの1レコードのうち、メンバー別パフォーマンス評価に必要な最小項目。"""

    project_id: str
    member: str
    status: str
    next_action_date: date | None = None


@dataclass(frozen=True)
class MemberActionRecord:
    """アクション管理DBの1レコードのうち、メンバー別パフォーマンス評価に必要な最小項目。"""

    project_id: str
    member: str
    action_type: str
    action_date: date


@dataclass(frozen=True)
class MemberPerformance:
    """メンバー1人分のパフォーマンス評価結果。"""

    member: str
    volume_contact_count: int
    volume_score: float
    quality_win_rate: float | None
    speed_compliance_rate: float | None
    overall_score: float | None


def member_contact_counts(actions: Sequence[MemberActionRecord]) -> dict[str, int]:
    """メンバーごとの期間内総接触回数を合算する（ボリューム指標の元データ）。

    `src.analytics.contact_count.count_total_contacts`と同じ対象アクション種別
    （COUNTABLE_ACTION_TYPES）を用いるが、集計単位を案件からメンバーに変える。
    集計対象外のアクション種別しか無いメンバーはキーに現れない
    （`.get(member, 0)`で0件として扱うこと）。
    """
    counts: dict[str, int] = {}
    for action in actions:
        if action.action_type in COUNTABLE_ACTION_TYPES:
            counts[action.member] = counts.get(action.member, 0) + 1
    return counts


def member_win_rates(projects: Sequence[MemberProjectRecord]) -> dict[str, float | None]:
    """メンバーごとの受注率（クオリティ指標）を算出する。

    分母は担当案件のうち決着済み（DECIDED_STATUSES＝契約済・失注・解約のいずれか）の件数、
    分子は契約済みの件数。決着済み案件が1件も無いメンバー（担当案件が全てアクティブ、または
    担当案件が0件）は受注率が未確定のためNoneを返す（0除算の回避、および"受注率0%"との
    混同を避けるため）。
    """
    decided: dict[str, int] = {}
    won: dict[str, int] = {}
    members: set[str] = set()
    for p in projects:
        members.add(p.member)
        if p.status in DECIDED_STATUSES:
            decided[p.member] = decided.get(p.member, 0) + 1
            if p.status == CONFIRMED_STATUS:
                won[p.member] = won.get(p.member, 0) + 1

    return {
        member: (won.get(member, 0) / decided[member] if decided.get(member) else None)
        for member in members
    }


def member_deadline_compliance_rates(
    projects: Sequence[MemberProjectRecord],
    actions: Sequence[MemberActionRecord],
    *,
    as_of: date,
) -> dict[str, float | None]:
    """メンバーごとの「次回アクション期限遵守率」（スピードの簡易代替指標）を算出する。

    ■ 簡易代替指標であることについて
    本来ガイドラインが求める「スピード」は一次返信時間等の実測値だが、案件管理DB・
    アクション管理DBには該当プロパティが存在しない（新規プロパティ追加は今回スコープ外）。
    そのため、既存プロパティのみで算出できる代替指標として、「次回アクション日を過ぎて
    いるのに、それ以降に当該案件への新しいアクションが記録されていない案件」の割合
    （期限超過率）の逆数（1 - 期限超過率）を「遵守率」として採用する。あくまで簡易近似
    であり、期限までに着手できたかどうかの二値判定に過ぎず、着手した速さそのもの
    （何時間で対応したか等）は測れない点に注意（詳細はdocs/member_performance_note.md参照）。

    分母は「決着済み（DECIDED_STATUSES＝契約済・失注・解約のいずれか）ではない案件のうち、
    次回アクション日がas_ofより過去（当日は含まない）の案件」のみ。決着済み案件は決着後に
    新しいアクションが発生しなくなるため、次回アクション日が未消化のまま過去日付で残って
    いても期限超過に含めない（含めるとスピード良く決着させた案件ほど遵守率を下げてしまう
    逆転が起きるため）。次回アクション日が未設定、または未来（as_of以降）の案件も判定対象外
    とする。分母が0件（次回アクション日が全て未来、担当案件に次回アクション日が設定されて
    いない、または担当案件が全て決着済み）のメンバーは遵守率が未確定のためNoneを返す。

    ■ フォロー実施の判定について（アクション種別は問わない）
    `followed_up`判定は`action_type`を一切フィルタしない（`member_contact_counts`の
    ボリューム集計がCOUNTABLE_ACTION_TYPESで人力メールを除外するのとは非対称）。これは
    意図的な設計であり、「期限を守れたかどうか」の判定においては、後で件数として数える
    活動量（ボリューム）とは異なり、メール等を含むどんな記録であっても「担当者が動いた
    事実」として扱ってよいという判断による。

    ■ アクションデータが完全に未連携の場合のフォールバック
    `actions`が空（1件も渡されていない）場合、分母（`due_count`）が存在するメンバーでも
    遵守率を`0.0`（確定した悪い実績）にはせず`None`（未確定）を返す。`actions`が空という
    状況は「実際に期限内フォローが1件もできなかった」のか「アクションデータが呼び出し側
    から渡されていない（連携未完了）」のか区別できないため、後者を誤って前者（悪い実績）
    と解釈してしまうのを避けるための安全策である。
    """
    action_dates_by_project: dict[str, list[date]] = {}
    for a in actions:
        action_dates_by_project.setdefault(a.project_id, []).append(a.action_date)

    overdue_count: dict[str, int] = {}
    due_count: dict[str, int] = {}
    members: set[str] = set()
    for p in projects:
        members.add(p.member)
        if p.status in DECIDED_STATUSES:
            continue
        if p.next_action_date is None or p.next_action_date >= as_of:
            continue
        due_count[p.member] = due_count.get(p.member, 0) + 1
        followed_up = any(
            action_date >= p.next_action_date
            for action_date in action_dates_by_project.get(p.project_id, [])
        )
        if not followed_up:
            overdue_count[p.member] = overdue_count.get(p.member, 0) + 1

    no_action_data_available = not actions

    rates: dict[str, float | None] = {}
    for member in members:
        due = due_count.get(member, 0)
        if not due:
            rates[member] = None
        elif no_action_data_available:
            rates[member] = None
        else:
            rates[member] = 1 - overdue_count.get(member, 0) / due
    return rates


def compute_member_performance(
    projects: Sequence[MemberProjectRecord],
    actions: Sequence[MemberActionRecord],
    *,
    as_of: date,
) -> tuple[MemberPerformance, ...]:
    """案件管理DB・アクション管理DBのメンバー別レコードから3指標＋総合スコアを算出する。

    ボリューム（件数）は受注率・遵守率（いずれも0〜1の割合）と異なりそのままでは
    掛け合わせられないため、算出対象メンバー内の相対値（グループ内最大接触回数に対する
    比率）で0〜1に正規化する。ボリュームの絶対目標値（週次アプローチ●件、等）は
    config等に外出しされておらず、DBスキーマへの新規プロパティ追加も今回のスコープ外の
    ため、この相対正規化を暫定の代替方法として採用する。この方式には「評価対象メンバーが
    1人だけの場合、volume_scoreが常に1.0になる（自分自身が最大値のため）」という
    相対評価特有の限界がある点に注意。

    総合スコア（overall_score）はガイドラインの「パフォーマンス＝スピード×ボリューム×
    クオリティ」の定義に忠実に3指標の掛け算とする。クオリティ・スピードいずれかが
    データ不足（担当案件0件、決着済み案件0件、期限判定対象0件等）でNoneの場合、
    "0点"と区別するため総合スコアもNoneとする（データが無いことを暗黙的に0点評価にしない）。
    グループ全体の総接触回数が0（メンバー全員が接触実績なし）の場合はvolume_scoreを
    一律0.0とする（0除算回避）。
    """
    members = sorted({p.member for p in projects} | {a.member for a in actions})

    contact_counts = member_contact_counts(actions)
    win_rates = member_win_rates(projects)
    compliance_rates = member_deadline_compliance_rates(projects, actions, as_of=as_of)

    max_contact_count = max(contact_counts.values(), default=0)

    results = []
    for member in members:
        count = contact_counts.get(member, 0)
        volume_score = count / max_contact_count if max_contact_count > 0 else 0.0
        quality = win_rates.get(member)
        speed = compliance_rates.get(member)
        overall = (
            volume_score * quality * speed
            if quality is not None and speed is not None
            else None
        )
        results.append(
            MemberPerformance(
                member=member,
                volume_contact_count=count,
                volume_score=volume_score,
                quality_win_rate=quality,
                speed_compliance_rate=speed,
                overall_score=overall,
            )
        )
    return tuple(results)
