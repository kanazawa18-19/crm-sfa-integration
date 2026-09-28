"""本人の登録意図を確認し、必須項目・重複確認→予約→1回だけ作成する。"""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timezone
import os
import logging
from typing import Any
from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.hub_creation.domain import CreationHeld, CreationNotApplicable, title_value, require_notion_fields, identity_hash, sheet_properties
from src.sync_engine.id_mapping import IdMapping
from src.sync_engine.record_sync_lock import acquire_record_sync_lock, RecordSyncBusy
from src.sync_engine.sync_notes import UNAVAILABLE, unresolved_note
from src.sync_engine.outbound_field_mapping import translate_properties, zoho_outbound_field_names, kintone_outbound_field_names
from src.sync_engine.outbound_value_mapping import translate_choice_value
from src.sync_engine.sync_headers import is_own_system_event, get_sync_system_id
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value, PARSEABLE_NOTION_PROPERTY_TYPES


logger = logging.getLogger(__name__)


class HubCreationService:
    def __init__(self, *, store, journal, notion_clients, adapters, enabled_since, sheet_gateway=None, project_link_prepare=None):
        self.project_link_prepare = project_link_prepare
        self.store, self.journal = store, journal
        self.notion_clients, self.adapters = notion_clients, adapters
        self.enabled_since, self.sheet_gateway = enabled_since, sheet_gateway

    def handle(self, event, *, scan_journal=None):
        from src.infrastructure.http_budget import http_budget
        with http_budget(240):
            return self._handle_with_budget(event, scan_journal=scan_journal)

    def _handle_with_budget(self, event, *, scan_journal=None):
        """通常更新はNone、新規を扱ったときは結果理由を返す。"""
        if is_own_system_event(event.sync_system_id, expected=get_sync_system_id()):
            return None
        if event.source_tool not in (Tool.NOTION, Tool.SPREADSHEET):
            return None
        registration = event.registration_key
        if event.source_tool == Tool.SPREADSHEET and not registration:
            return None
        source_key = "sheet:" + registration if registration else "notion:" + event.external_id
        if not registration:
            source_key = self.journal.source_for_page(event.external_id) or source_key
        lock_id = source_key
        with acquire_record_sync_lock(self.store, event.db_key, lock_id):
            pending_targets = set()
            try:
                outcome = self._handle_locked(event, source_key, pending_targets)
            except Exception as exc:
                if scan_journal is not None and getattr(exc, 'status_code', None) == 404:
                    scan_journal.hold(source_key, '登録元が見つかりません。確認後に登録元を再通知してください')
                raise
            if scan_journal is not None:
                # 元ページの同期ロック内で確定し、後から届いた手動再通知を上書きしない。
                attempt = self.journal.get(source_key, 'zoho')
                if outcome in (None, 'new_record_archived') or (attempt and attempt['state'] in ('created', 'reserved')):
                    scan_journal.done(source_key)
                elif 'zoho' not in pending_targets:
                    scan_journal.hold(source_key, '重複照合以外の確認待ちです。登録元のメモを確認し、解決後に再通知してください')
            return outcome

    def retry_after_sheet_sync(self, event, result):
        """通常同期でNotionへ届いた後だけ、登録履歴のある行を再判定する。"""
        if event.source_tool != Tool.SPREADSHEET or not event.source_notion_key or result.skipped:
            return
        if any(Tool.NOTION in item.skipped_tools for item in result.properties):
            return
        fallback_key = "notion:" + event.source_notion_key
        source_key = fallback_key
        try:
            source_key = self.journal.source_for_page(event.source_notion_key) or fallback_key
            if not source_key.startswith("sheet:"):
                return
            retry = replace(event, source_tool=Tool.NOTION, external_id=event.source_notion_key,
                            properties={}, registration_key=None, source_notion_key=None)
            self.handle(retry)
            self.journal.dismiss_hold(source_key, "retry")
            self.journal.dismiss_hold(fallback_key, "retry")
        except Exception:
            # 通常編集は既に同期済み。再判定の障害だけで成功済み編集を5xxに戻さない。
            logger.warning("シート編集の同期は完了しましたが、新規登録の再判定を保留しました")
            try:
                self.journal.hold(source_key, "retry", event.db_key, "通常編集は同期済みです。新規登録の再判定を確認してください")
            except Exception:
                logger.warning("新規登録の再判定保留を保存できませんでした")

    def _handle_locked(self, event, source_key, pending_targets):
        client = self.notion_clients.get(event.db_key)
        if client is None:
            return "new_record_notion_unavailable"
        sheet = get_schema(event.db_key).spreadsheet_sheet_name
        is_sheet = bool(event.registration_key)
        origin = "spreadsheet" if source_key.startswith("sheet:") else "notion"
        page_id = None
        properties = {}
        try:
            if is_sheet:
                if self.sheet_gateway is None:
                    raise CreationHeld("シート登録用の接続がありません")
                attempt = self.journal.get(source_key, "notion")
                accepted_key = attempt["externalId"] if attempt and attempt["state"] == "created" else None
                values, _metadata, _row = self.sheet_gateway.read(sheet, event.registration_key, accepted_key=accepted_key)
                properties = sheet_properties(event.db_key, values)
                name, title = title_value(event.db_key, properties)
                fingerprint = identity_hash(event.db_key, title)
                attempt = self.journal.get(source_key, "notion")
                if attempt and attempt["state"] == "created":
                    page_id = attempt["externalId"]
                elif attempt and attempt["state"] == "reserved":
                    raise CreationHeld("Notionへの作成結果が不明です。再作成せず確認を待っています")
                else:
                    self._check_notion_duplicates(client, name, title)
                    if not self.journal.reserve(source_key, "notion", event.db_key, fingerprint):
                        raise CreationHeld("同じ名前の登録が進行中、または既に登録されています")
                    # 予約後の失敗はreservedのまま残し、二度目のPOSTを禁止する。
                    page_id = client.create_page_once(properties)
                    self.journal.finish(source_key, "notion", page_id)
            else:
                existing = self.store.get(event.external_id)
                if existing is not None and not self.journal.has_source(source_key):
                    return None
                if existing is not None and all(
                    getattr(existing, {"zoho": "zoho_id", "kintone": "kintone_id"}[adapter.target])
                    or (adapter.target == "kintone" and event.db_key not in {"client_master", "project", "action"})
                    for adapter in self.adapters
                ):
                    self.journal.dismiss_hold(source_key, "source")
                    return None
                raw = client.get_raw_page(event.external_id)
                if raw.get("archived") or raw.get("in_trash"):
                    return "new_record_archived"
                parent = raw.get("parent", {}).get("database_id", "").replace("-", "")
                if parent != get_schema(event.db_key).notion_database_id.replace("-", ""):
                    return "new_record_database_mismatch"
                created = datetime.fromisoformat(raw["created_time"].replace("Z", "+00:00"))
                if created < self.enabled_since:
                    return None  # 過去の未登録は週次点検だけに残す。
                page_id = event.external_id
                properties = self._creation_properties(event.db_key, raw)
                require_notion_fields(event.db_key, properties)
                name, title = title_value(event.db_key, properties)
                fingerprint = identity_hash(event.db_key, title)
                self._check_notion_duplicates(client, name, title, page_id)

            with acquire_record_sync_lock(self.store, event.db_key, page_id):
                creation_page_snapshot = client.get_raw_page(page_id) if is_sheet else raw
                mapping = self.store.get(page_id)
                if mapping is None:
                    # 対応表だけできて排他待ちになっても、再送で過去の取込済みと誤認しない。
                    self.journal.hold(source_key, "source", event.db_key, "新規登録の外部反映を処理中です")
                    mapping = IdMapping(notion_key=page_id, db_key=event.db_key)
                    self.store.upsert(mapping, expected_last_synced_at=None)
                if mapping.db_key != event.db_key:
                    raise CreationHeld("対応表のDBが一致しません")
                if event.db_key == 'action':
                    from src.hub_creation.creation_payload import creation_rollup
                    action_page = client.get_raw_page(page_id) if is_sheet else raw
                    for name in ('担当営業', '提案サービス'):
                        properties.pop(name, None)
                        value = creation_rollup(action_page.get('properties', {}).get(name))
                        if value is not None:
                            properties[name] = value
                if event.db_key == "project" and self.project_link_prepare is not None:
                    # Notion登録・対応表成立後、CRM作成・シート配送・メモより先に保持する。
                    self.project_link_prepare(mapping)
                self.journal.resolve_hold(source_key, "source", page_id)
                notes = {f"{UNAVAILABLE}|{origin}:新規登録": ""}
                for adapter in self.adapters:
                    if adapter.target == "kintone" and event.db_key not in {"client_master", "project", "action"}:
                        continue
                    target_tool = Tool(adapter.target)
                    scoped = {p.name: properties[p.name] for p in get_schema(event.db_key).properties
                              if p.name in properties and p.should_sync_to(target_tool)
                              and properties[p.name] not in (None, "", [], {})}
                    table = zoho_outbound_field_names() if target_tool == Tool.ZOHO else kintone_outbound_field_names()
                    _, dropped = translate_properties(table, event.db_key, scoped,
                                                      translate_choice_value if target_tool == Tool.ZOHO else None)
                    for name in dropped:
                        key, value = unresolved_note(Tool(origin), name, scoped[name])
                        notes[key] = value + " 未同期の送り先: " + adapter.target + "。"
                held = []
                for adapter in self.adapters:
                    target = adapter.target
                    column = {"zoho": "zoho_id", "kintone": "kintone_id"}[target]
                    note_key = f"{UNAVAILABLE}|{origin}:新規登録（{target}）"
                    try:
                        # 外部POST直後の返信通知より先に、元ページとの対応を確定する。
                        with acquire_record_sync_lock(self.store, event.db_key, "hub-create:" + target):
                            mapping = self.store.get(page_id)
                            if mapping is None or mapping.db_key != event.db_key:
                                raise CreationHeld("作成中の対応表を確認してください")
                            if getattr(mapping, column):
                                notes[note_key] = ""
                                continue
                            attempt = self.journal.get(source_key, target)
                            if attempt and attempt["state"] == "created":
                                external_id = attempt["externalId"]
                            elif attempt and attempt["state"] == "reserved":
                                raise CreationHeld("作成結果が不明です。自動で再作成せず確認を待っています")
                            else:
                                from src.record_merge.creation_candidates import candidate_context
                                with candidate_context(event.db_key, page_id, source_key, properties):
                                    payload = adapter.plan(event.db_key, properties)
                                    scan = getattr(adapter, 'duplicate_scan', None)
                                    if scan is not None and scan.exists():
                                        # 分割照合中の編集・アーカイブを、古い入力のまま送らない。
                                        from src.record_merge.domain import digest
                                        latest = client.get_raw_page(page_id)
                                        if (latest.get('archived') or latest.get('in_trash')
                                                or latest.get('parent') != creation_page_snapshot.get('parent')
                                                or digest(self._creation_properties(event.db_key, latest))
                                                != digest(self._creation_properties(event.db_key, creation_page_snapshot))):
                                            raise CreationHeld('照合中に登録元が変更されました。現在の入力で再確認します')
                                    from src.infrastructure.http_budget import remaining, HttpBudgetExceeded
                                    from src.hub_creation.duplicate_scan import PENDING
                                    try:
                                        left = remaining()
                                        if left is not None and left <= 40:
                                            raise HttpBudgetExceeded()
                                    except HttpBudgetExceeded:
                                        if scan is not None:
                                            scan.defer()
                                            raise CreationHeld(PENDING) from None
                                        raise CreationHeld('送信前に時間枠へ到達しました。登録元を再通知してください') from None
                                if not self.journal.reserve(source_key, target, event.db_key, fingerprint):
                                    raise CreationHeld("同じ名前の登録が進行中、または既に登録されています")
                                external_id = adapter.create(event.db_key, payload)
                                self.journal.finish(source_key, target, external_id)
                            updated = replace(mapping, **{column: external_id})
                            self.store.upsert(updated, expected_last_synced_at=mapping.last_synced_at)
                            mapping = updated
                            self.journal.dismiss_hold(source_key, "mapping:" + target)
                            notes[note_key] = ""
                    except RecordSyncBusy:
                        # 別件の作成中は失敗保留にせず、Webhook・キューの再送へ戻す。
                        raise
                    except CreationNotApplicable as exc:
                        self.journal.dismiss_hold(source_key, target)
                        notes[note_key] = f"[{origin}:新規登録（{target}）] 対象外: {exc}。この送り先への登録は不要です。"
                    except CreationHeld as exc:
                        reason = str(exc)
                        from src.hub_creation.duplicate_scan import PENDING
                        if reason == PENDING:
                            pending_targets.add(target)
                        self.journal.hold(source_key, target, event.db_key, reason)
                        notes[note_key] = f"[{origin}:新規登録（{target}）] {reason}。既存候補への自動紐付けは行っていません。"
                        held.append(target + ": " + reason)
                    except Exception:
                        # 認証情報やAPIレスポンス本文をメモへ出さない。
                        reason = "通信・保存の確認が必要です。予約済みの作成は自動で再実行しません"
                        self.journal.hold(source_key, target, event.db_key, reason)
                        attempt = self.journal.get(source_key, target)
                        if attempt and attempt["state"] == "created":
                            self.journal.hold(source_key, "mapping:" + target, event.db_key, "外部作成済みですが対応表の保存確認が必要です")
                        notes[note_key] = f"[{origin}:新規登録（{target}）] {reason}。"
                        held.append(target + ": " + reason)
                client.upsert_sync_notes(page_id, notes)
                if origin == "spreadsheet" and self.sheet_gateway is not None:
                    registration_key = source_key.removeprefix("sheet:")
                    status = "Notion登録済み / " + ("・".join(held) if held else "外部登録完了")
                    row = self.sheet_gateway.update(sheet, registration_key, notion_key=page_id, status=status)
                    updated = replace(mapping, spreadsheet_row=row)
                    self.store.upsert(updated, expected_last_synced_at=mapping.last_synced_at)
                return "hub_creation_held" if held else "hub_creation_complete"
        except CreationHeld as exc:
            reason = str(exc)
            self.journal.hold(source_key, "source", event.db_key, reason)
            if page_id:
                key = f"{UNAVAILABLE}|{origin}:新規登録"
                client.upsert_sync_notes(page_id, {key: f"[{origin}:新規登録] {reason}。"})
            if origin == "spreadsheet" and self.sheet_gateway is not None:
                registration_key = source_key.removeprefix("sheet:")
                try:
                    self.sheet_gateway.update(sheet, registration_key, notion_key=page_id, status="保留: " + reason)
                except CreationHeld:
                    pass  # 行の一意性が失われた場合は触らず、保存済みの保留を週次通知する。
            return "hub_creation_held"

    @staticmethod
    def _creation_properties(db_key, raw):
        """作成に用いる入力だけを抽出し、直前照合にも同じ規則を使う。"""
        properties = {}
        for name, value in raw.get('properties', {}).items():
            try:
                prop = get_schema(db_key).get_property(name)
            except KeyError:
                continue
            if (prop.is_writable and prop.sync_scope.synced_tools
                    and value.get('type') in PARSEABLE_NOTION_PROPERTY_TYPES):
                properties[name] = parse_notion_property_value(value)
        if db_key == 'action':
            from src.hub_creation.creation_payload import creation_rollup
            for name in ('担当営業', '提案サービス'):
                properties.pop(name, None)
                value = creation_rollup(raw.get('properties', {}).get(name))
                if value is not None:
                    properties[name] = value
        return properties

    @staticmethod
    def _check_notion_duplicates(client, name, title, self_id=None):
        response = client.query_raw({"page_size": 2, "filter": {"property": name, "title": {"equals": title}}})
        if response.get("has_more") or any(p["id"].replace("-", "") != (self_id or "").replace("-", "") for p in response["results"]):
            raise CreationHeld("Notionに同名の候補があります。重複かどうかの確認が必要です")


def enabled_since():
    raw = os.environ.get("HUB_CREATION_ENABLED_SINCE")
    if not raw:
        return None
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("HUB_CREATION_ENABLED_SINCEにはタイムゾーンが必要です")
    return value.astimezone(timezone.utc)
