"""同期エンジン内部で扱う変更イベントの統一データ構造。

各ツールのWebhookハンドラ（webhook_handlers/）は、ツール固有のペイロードをこの
SyncEvent へ変換したうえでdispatcherへ渡す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.db_schema.base import Tool


@dataclass(frozen=True)
class SyncEvent:
    """どのツールで・どのレコードが・どのプロパティが・いつ変更されたか、を表す。"""

    source_tool: Tool
    db_key: str  # src.db_schema.registry の DatabaseSchema.key
    external_id: str  # source_tool側でのレコードID（Notion起点ならnotion_key）
    occurred_at: datetime  # 変更発生日時（各ツールのupdated_at相当）。あいまいさを避けるため必須とする
    properties: dict[str, Any] = field(default_factory=dict)  # 変更後のプロパティ名 -> 値
    # Webhookペイロードに X-Sync-System-ID ヘッダーが含まれていた場合のその値。
    # 無限ループ防止（sync_headers.is_own_system_event）の判定に使う。
    sync_system_id: str | None = None
