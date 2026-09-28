"""削除承認の状態遷移。DB・外部APIを起動しない。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class ReviewConflict(ValueError):
    """表示後に状態が変化したため、再確認が必要。"""


class ReviewForbidden(PermissionError):
    """マネージャーだけが判断できる。"""


def is_blank(value: Any) -> bool:
    """0とFalseを削除と取り違えない。"""
    return value is None or value == "" or isinstance(value, (list, tuple, dict)) and not value


def snapshot_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ReviewDecision:
    state: str
    action: str


def decide(*, state: str, action: str, is_manager: bool,
           expected_revision: int, actual_revision: int) -> ReviewDecision:
    """1回目と2回目を別の状態・版として扱い、同時送信での飛越しを防ぐ。"""
    if not is_manager:
        raise ReviewForbidden("マネージャーだけが処理できます")
    if expected_revision != actual_revision:
        raise ReviewConflict("表示後に処理されました。読み直してください")
    transitions = {
        ("pending", "confirm"): "confirmed",
        ("confirmed", "approve"): "approved",
        ("pending", "keep_blank"): "kept_blank",
        ("confirmed", "keep_blank"): "kept_blank",
        ("pending", "restore"): "restore_requested",
        ("confirmed", "restore"): "restore_requested",
        ("failed", "resume"): "approved",
        ("failed", "recheck"): "pending",
        ("kept_blank", "recheck"): "pending",
        ("approved", "resume"): "approved",
        ("restore_requested", "resume"): "restore_requested",
    }
    following = transitions.get((state, action))
    if following is None:
        raise ReviewConflict("この状態では選択した操作を実行できません")
    return ReviewDecision(state=following, action=action)


def values_equal(left, right):
    if is_blank(left) and is_blank(right):
        return True
    return snapshot_hash({"v": left}) == snapshot_hash({"v": right})
