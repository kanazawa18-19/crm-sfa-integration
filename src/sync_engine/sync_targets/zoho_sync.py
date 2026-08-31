"""Zoho CRM向け同期ターゲット（過渡期CRM。ENABLE_ZOHO=Falseで疎結合に切り離せる）。

01_システム構成「疎結合設計」：ENABLE_ZOHOをFalseに変更するだけで、他システムに
一切影響を与えずZoho連携を切り離せること。本モジュールでは全メソッドの冒頭で
ENABLE_ZOHOを判定し、無効時はZohoClientを一切呼び出さずスキップする。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.outbound_value_mapping import translate_choice_value
from src.sync_engine.outbound_field_mapping import (
    translate_properties,
    zoho_outbound_field_names,
)
from src.sync_engine.sync_targets.base import SyncTarget

logger = logging.getLogger(__name__)

# **★ この項目はZoho側に存在しない**（2026-08-31、config/zoho_field_mapping.jsonで全項目を確認）。
# つまり論理削除は現状どのツールでも成立しない。ただし呼び出し元が無く（Dispatcherは
# Notionに対してのみdelete_record()を呼ぶ）、実害は出ていない休眠経路。
# ここを配線する前に、Zoho側へ削除フラグ相当の項目を作るか、別の削除方式を決めること。
_DELETE_FLAG_FIELD = "削除フラグ"


class ZohoClient(Protocol):
    """Zoho CRM APIの最小インターフェース。実HTTP通信は本Protocolの実装側が担う。"""

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None: ...

    def insert_record(self, module: str, record: dict[str, Any]) -> str:
        """レコードを新規登録し、採番されたIDを返す。"""
        ...

    def update_record(
        self,
        module: str,
        record_id: str,
        record: dict[str, Any],
        *,
        expected_version: str | None = None,
    ) -> None: ...


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

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        if not self._enabled:
            return None
        try:
            return self._client.get_record(self._module, external_id)
        except Exception:
            # 2026-08-27本番障害対応（kintone_sync.KintoneSyncTarget.get_record()と同じ
            # 理由）: 例外はここでは握らず伝播させ、呼び出し元（Dispatcher）にスキップ判断を
            # 委ねる。切り分けに必要なmodule/external_id/db_keyのみをここで記録する
            # （レコードの中身は出さない）。
            logger.exception(
                "ZohoSyncTarget.get_record failed (module=%r, external_id=%r, db_key=%r)",
                self._module,
                external_id,
                db_key,
            )
            raise

    def upsert_record(
        self,
        external_id: str | None,
        properties: dict[str, Any],
        *,
        db_key: str | None = None,
        expected_version: str | None = None,
    ) -> str | None:
        if not self._enabled:
            # **更新でもNoneを返す**（2026-09-01、shirokuma-secレビュー指摘）。
            # 外部IDを返すと、呼び出し元が「書き込み成功」と数えてしまう。
            # 実際には1件も書いていないので、スキップとして扱わせる。
            return None
        payload = self._to_zoho_payload(properties, db_key)
        if payload is None:
            # 1項目も送っていないので、更新であっても「書き込めていない」を返す。
            # ここでexternal_idを返すとDispatcher._write_value()が「書き込み成功」と数え、
            # まさにこの変更が無くそうとしている「スキップが成功に見える」状態に戻る。
            return None
        if external_id is None:
            return self._client.insert_record(self._module, payload)
        self._client.update_record(
            self._module, external_id, payload, expected_version=expected_version
        )
        return external_id

    def unsupported_properties(
        self, properties: dict[str, Any], *, db_key: str | None = None
    ) -> frozenset[str]:
        """このツールへ送れないプロパティ名。呼び出し元が「書けなかった」と判定するのに使う。

        1回の書き込みに複数プロパティをまとめると、戻り値だけでは
        「どの項目が落ちたか」が分からない。書く前にここで聞く。
        """
        _payload, unmapped = translate_properties(
            zoho_outbound_field_names(), db_key, properties, translate_choice_value
        )
        return frozenset(unmapped)

    def _to_zoho_payload(
        self, properties: dict[str, Any], db_key: str | None
    ) -> dict[str, Any] | None:
        """Notionのプロパティ名をZohoのapi_nameへ置き換える。

        2026-08-31の棚卸しで、ここへNotionのプロパティ名がそのまま渡り、そのままAPIへ
        送られていたことが判明した（Zohoのapi_nameは`field7`のような自動採番なので、
        名前が一致するのは104項目中1項目だけだった）。詳細は
        `src/sync_engine/outbound_field_mapping.py`。

        送り先を決められない項目は**送らない**。1項目も残らなければNoneを返し、
        呼び出し元に「書き込んでいない」ことを伝える（空のレコードでAPIを叩かない）。
        """
        payload, unmapped = translate_properties(
            zoho_outbound_field_names(), db_key, properties, translate_choice_value
        )
        if unmapped:
            logger.warning(
                "ZohoSyncTarget: Zoho側の項目が特定できないため送信しません "
                "(module=%r, db_key=%r, properties=%r)",
                self._module,
                db_key,
                unmapped,
            )
        return payload or None

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        if not self._enabled:
            return
        self._client.update_record(self._module, external_id, {_DELETE_FLAG_FIELD: True})
