"""Zoho CRM向け同期ターゲット（過渡期CRM。ENABLE_ZOHO=Falseで疎結合に切り離せる）。

01_システム構成「疎結合設計」：ENABLE_ZOHOをFalseに変更するだけで、他システムに
一切影響を与えずZoho連携を切り離せること。本モジュールでは全メソッドの冒頭で
ENABLE_ZOHOを判定し、無効時はZohoClientを一切呼び出さずスキップする。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.sync_targets.base import SyncTarget

_DELETE_FLAG_FIELD = "削除フラグ"


class ZohoClient(Protocol):
    """Zoho CRM APIの最小インターフェース。実HTTP通信は本Protocolの実装側が担う。"""

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None: ...

    def insert_record(self, module: str, record: dict[str, Any]) -> str:
        """レコードを新規登録し、採番されたIDを返す。"""
        ...

    def update_record(self, module: str, record_id: str, record: dict[str, Any]) -> None: ...


def is_zoho_enabled() -> bool:
    """環境変数ENABLE_ZOHOを読み判定する。未設定時はTrue（有効）扱い。"""
    raw = os.environ.get("ENABLE_ZOHO")
    if raw is None:
        return True
    return raw.strip().lower() not in ("false", "0", "no", "")


class ZohoSyncTarget(SyncTarget):
    """moduleはZoho側のモジュール名（例:「案件」）。DBごとにインスタンス化する。"""

    tool = Tool.ZOHO

    def __init__(self, client: ZohoClient, module: str, *, enabled: bool | None = None) -> None:
        self._client = client
        self._module = module
        # enabled未指定時は呼び出しごとに環境変数を再評価する
        # （テストでのmonkeypatchによる切り替えにも追従できるようにするため）。
        self._enabled_override = enabled

    @property
    def _enabled(self) -> bool:
        return self._enabled_override if self._enabled_override is not None else is_zoho_enabled()

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        if not self._enabled:
            return None
        return self._client.get_record(self._module, external_id)

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        if not self._enabled:
            # 「作成されていない」ことを型で表現する（""だと採番済みIDと誤認されうるため）。
            return external_id
        if external_id is None:
            return self._client.insert_record(self._module, properties)
        self._client.update_record(self._module, external_id, properties)
        return external_id

    def delete_record(self, external_id: str) -> None:
        if not self._enabled:
            return
        self._client.update_record(self._module, external_id, {_DELETE_FLAG_FIELD: True})
