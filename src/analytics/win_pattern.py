"""勝ちパターン分析・クロスセル対象抽出（06_営業分析ロジック）。

勝ちパターン分析は「何回目の商談で・どのサービス構成を提案したか」の組み合わせ単位で
受注率を算出する。受注率の算出には分母として受注・失注の両方の案件が必要なため、
（受注案件だけでなく）提案実績のある全案件を入力として受け取る。

クロスセル対象抽出は、取引先ごとに契約中サービスと提案済みサービスの集合を突合し、
未提案のサービスを一覧化する。サービス・商品DBの「クロスセル対象基準」（自由記述の
定性的な条件）まで加味した判定はルールベースの純粋関数の範囲を超えるため対象外とし、
ここでは「未提案サービスの突合」までを担う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ProposalRecord:
    """ある案件の商談時点で、何回目にどのサービス構成を提案したかを表す。"""

    project_id: str
    meeting_number: int
    services: frozenset[str]
    is_won: bool


@dataclass(frozen=True)
class WinPattern:
    """(商談回数, サービス構成) の組み合わせ単位の受注率。"""

    meeting_number: int
    services: frozenset[str]
    sample_size: int
    win_rate: float


def analyze_win_patterns(
    records: Sequence[ProposalRecord], *, min_sample_size: int = 3
) -> list[WinPattern]:
    """(商談回数, サービス構成)ごとに受注率を算出し、受注率の高い順に並べて返す。

    min_sample_sizeでサンプル数が少なすぎる組み合わせ（1件だけ受注して受注率100%、等の
    ノイズ）を除外できる。デフォルトは3（サンプル数1〜2件のノイズが週報等にそのまま
    出てしまうのを防ぐための最小値）。
    """
    groups: dict[tuple[int, frozenset[str]], list[bool]] = {}
    for record in records:
        key = (record.meeting_number, record.services)
        groups.setdefault(key, []).append(record.is_won)

    patterns = [
        WinPattern(
            meeting_number=key[0],
            services=key[1],
            sample_size=len(outcomes),
            win_rate=sum(outcomes) / len(outcomes),
        )
        for key, outcomes in groups.items()
        if len(outcomes) >= min_sample_size
    ]
    return sorted(patterns, key=lambda p: p.win_rate, reverse=True)


@dataclass(frozen=True)
class ClientServiceStatus:
    """取引先ごとの契約中サービス・提案済みサービス。"""

    client_id: str
    contracted_services: frozenset[str]
    proposed_services: frozenset[str]


def extract_cross_sell_targets(
    clients: Sequence[ClientServiceStatus],
    all_services: Iterable[str],
) -> dict[str, frozenset[str]]:
    """取引先ごとに、契約中でも提案済みでもないサービス（クロスセル対象）を抽出する。

    未提案サービスが無い取引先はキーに現れない。
    """
    catalog = frozenset(all_services)
    targets: dict[str, frozenset[str]] = {}
    for client in clients:
        excluded = client.contracted_services | client.proposed_services
        untapped = catalog - excluded
        if untapped:
            targets[client.client_id] = untapped
    return targets
