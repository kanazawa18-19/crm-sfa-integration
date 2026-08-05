"""X-Sync-System-ID ヘッダーの生成・検証（05_同期・競合制御「無限ループ防止」）。

同期エンジンが各ツールAPIへ書き込みを行う際にこのヘッダーを付与することで、
「同期エンジン自身の書き込みによって発生したWebhook」を検知し、再処理（無限ループ）を防ぐ。
"""

from __future__ import annotations

import os

HEADER_NAME = "X-Sync-System-ID"
_DEFAULT_SYNC_SYSTEM_ID = "自社CRM-Engine"


def get_sync_system_id() -> str:
    """config/.env の SYNC_SYSTEM_ID を返す（未設定時はデフォルト値）。"""
    return os.environ.get("SYNC_SYSTEM_ID") or _DEFAULT_SYNC_SYSTEM_ID


def build_sync_headers(*, system_id: str | None = None) -> dict[str, str]:
    """各ツールAPIへの書き込みリクエストに付与するヘッダーを生成する。"""
    return {HEADER_NAME: system_id or get_sync_system_id()}


def is_own_system_event(received_system_id: str | None, *, expected: str | None = None) -> bool:
    """WebhookペイロードのX-Sync-System-IDヘッダー値が自システムのものかどうかを判定する。

    ヘッダー自体が無い（None・空文字）場合は、人手または外部ツール自身による本来の変更
    であるとみなしFalseを返す。
    """
    if not received_system_id:
        return False
    return received_system_id == (expected or get_sync_system_id())
