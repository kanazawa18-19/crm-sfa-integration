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
from src.sync_engine.owner_mapping import owner_payload
from src.sync_engine.decided_choices import CONTROLLERS, choices, merge_choice_memo, replace_memo_body
from src.sync_engine.clients._http import ApiError, ConcurrentModificationError
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

    def __init__(self, client: ZohoClient, module: str, *, enabled: bool | None = None, get_mapping=None) -> None:
        self._get_mapping = get_mapping
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
            record = self._client.get_record(self._module, external_id)
            if record is None:
                return None
            from src.sync_engine.zoho_text_limits import normalized_text_record
            try:
                return normalized_text_record(db_key, record)
            except ValueError as exc:
                raise ApiError(422, str(exc)) from None
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
        values = choices(properties.get("サイトコントローラー"), CONTROLLERS) if db_key == "project" else None
        memo_edit = db_key == "project" and payload is not None and "field70" in payload
        if values or memo_edit:
            current = self._client.get_record(self._module, external_id) if external_id else {}
            if current is None:
                raise ApiError(404, "備考の読取先がありません")
            version = current.get("Modified_Time")
            if external_id and (not version or (expected_version is not None and version != expected_version)):
                raise ConcurrentModificationError(409, "備考の版が変更されています")
            try:
                memo = current.get("field70", "")
                if memo_edit:
                    memo = replace_memo_body(memo, payload["field70"], limit=None)
                if values:
                    memo = merge_choice_memo(memo, "サイトコントローラー", values, limit=None)
            except ValueError as exc:
                raise ApiError(422, str(exc)) from None
            payload = {**(payload or {}), "field70": memo}
            if external_id:
                expected_version = version
        if payload is None:
            # 1項目も送っていないので、更新であっても「書き込めていない」を返す。
            # ここでexternal_idを返すとDispatcher._write_value()が「書き込み成功」と数え、
            # まさにこの変更が無くそうとしている「スキップが成功に見える」状態に戻る。
            return None
        from src.sync_engine.zoho_text_limits import limit_payload, text_fields, record_notes
        fields = self._text_metadata() if text_fields(db_key, payload) else []
        if values and not memo_edit:
            from src.sync_engine.decided_choices import split_controller_memo
            metadata = next((field for field in fields if field.get('api_name') == 'field70'), {})
            limit = metadata.get('length')
            previous = current.get('field70') or ''
            if (type(limit) is int and len(previous) >= limit
                    and len(split_controller_memo(previous)[1]) != len(split_controller_memo(payload['field70'])[1])):
                raise ApiError(422, '短縮済みメモの管理枠を変更するにはNotionのメモ本文も同時に再送してください')
        try:
            payload = limit_payload(db_key, payload, fields)
        except ValueError as exc:
            raise ApiError(422, str(exc)) from None
        if external_id is None:
            external_id = self._client.insert_record(self._module, payload)
        else:
            self._client.update_record(
                self._module, external_id, payload, expected_version=expected_version
            )
        # 管理枠だけの更新では、以前の本文省略の記録を解除しない。
        record_notes({key: value for key, value in payload.text_limit_notes.items()
                      if value or not (values and not memo_edit)})
        from src.sync_engine.zoho_text_limits import ambiguity_key
        record_notes({ambiguity_key(name): '' for name in text_fields(db_key, payload)
                      if name in properties})
        return external_id

    def _text_metadata(self):
        reader = getattr(self._client, "get_fields", None)
        from src.sync_engine.zoho_text_limits import cached_metadata
        return cached_metadata(self._client, self._module, reader) if reader is not None else []

    def is_shortened_echo(self, name, incoming, original, *, db_key=None, external_id=None):
        from src.sync_engine.zoho_text_limits import TEXT_PROPERTIES, limit_payload
        if (name not in TEXT_PROPERTIES.get(db_key, ()) or not isinstance(incoming, str)
                or not isinstance(original, str) or len(original) <= len(incoming)):
            return False
        code = zoho_outbound_field_names().get(db_key, {}).get(name)
        value = original
        if db_key == 'project':
            current = self._client.get_record(self._module, external_id)
            if current is None:
                raise ApiError(404, "管理用メモの読取先がありません")
            value = replace_memo_body(current.get(code), original,
                                      limit=None)
        try:
            projected = limit_payload(db_key, {code: value}, self._text_metadata())[code]
        except ValueError as exc:
            raise ApiError(422, str(exc)) from None
        if db_key == 'project':
            from src.sync_engine.decided_choices import split_controller_memo
            projected = split_controller_memo(projected)[0]
        return projected != original and incoming == projected

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
        _, handled = self._owner_payload(db_key, properties)
        _, additional = self._confirmed_payload(db_key, properties)
        return frozenset(set(unmapped) - handled - additional)

    def _confirmed_payload(self, db_key, properties):
        """確定済み単一関連と、実設定で確認した真偽値だけを部分更新する。"""
        from src.hub_creation.creation_payload import ZOHO_RELATIONS
        payload, handled = {}, set()
        if db_key in {"contact", "project"} and self._get_mapping is not None:
            for code, (name, target_db) in ZOHO_RELATIONS[db_key].items():
                ids = properties.get(name)
                if not isinstance(ids, (list, tuple)) or len(ids) != 1 or not isinstance(ids[0], str):
                    continue
                mapping = self._get_mapping(ids[0])
                if mapping is None or mapping.db_key != target_db or not mapping.zoho_id:
                    continue
                payload[code] = {"id": str(mapping.zoho_id)}
                handled.add(name)
        if db_key == "project":
            for name, code in {"かつやさん": "field16", "問合せ": "field48"}.items():
                if type(properties.get(name)) is bool:
                    payload[code] = properties[name]
                    handled.add(name)
        return payload, handled

    def _owner_payload(self, db_key, properties):
        owners, handled = owner_payload(db_key, properties, "zoho")
        if owners.get('Owner', 'not_sent') is None:
            from src.sync_engine.owner_mapping import zoho_owner_required
            try:
                required = zoho_owner_required(self._client, self._module)
            except Exception:
                return {}, set()
            return owner_payload(db_key, properties, "zoho", required=required)
        return owners, handled

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
        owners, handled = self._owner_payload(db_key, properties)
        payload.update(owners)
        extra, additional = self._confirmed_payload(db_key, properties)
        payload.update(extra)
        unmapped = sorted(set(unmapped) - handled - additional)
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
