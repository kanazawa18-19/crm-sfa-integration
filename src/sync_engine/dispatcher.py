"""Any-to-Any 同期ディスパッチャ（05_同期・競合制御）。

SyncEvent（どのツールで・どのレコードが・どのプロパティが・いつ変更されたか）を受け取り、

  1. 送信元ツールを対象リストから除外（Self-Exclusion）
  2. X-Sync-System-ID ヘッダーが自システムのものであれば処理をスキップ（無限ループ防止）
  3. IDマッピングストアでレコードを特定
  4. PropertyDefinition.sync_scope を見て、そのプロパティが同期対象かどうか判定
  5. コンフリクトの疑いがあれば conflict_resolver に判定を委ね、結果に応じて各SyncTargetへ書き込み

の順で処理する。

差分更新の原則（05_同期・競合制御「大量データ対策」）：本ディスパッチャはWebhookで届く
SyncEvent を1件ずつ即時処理する設計であり、event.occurred_at が該当レコードの
last_synced_at より新しい場合のみ処理する（既に反映済みの古いイベントは無視する）。
「最終更新日時が前回同期より新しいレコードのみを対象とする」大量データ対策・
Notion APIページング（100件/回）への対応は、この即時処理では通常問題にならず、
定時バッチ側（同期漏れの自己修復、後続タスク）で考慮する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
from src.sync_engine.conflict_resolver import (
    ConflictResolution,
    RejectedData,
    ResolutionAction,
    ToolValue,
    resolve_conflict,
)
from src.sync_engine.id_mapping import IdMapping, IdMappingStore
from src.sync_engine.slack_notifier import SlackNotifier
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import is_own_system_event
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.sync_targets.spreadsheet_sync import SpreadsheetSyncTarget

logger = logging.getLogger(__name__)

_ALL_TOOLS: tuple[Tool, ...] = (Tool.NOTION, Tool.SPREADSHEET, Tool.KINTONE, Tool.ZOHO)


@dataclass(frozen=True)
class PropertyDispatchResult:
    """1プロパティ分の処理結果（テスト・ログ用）。

    written_tools/skipped_toolsはいずれも「本来書き込む意図があったツール」
    （sync_scope・コンフリクト解決結果で書き込み対象と判定されたツール）の内訳。
    written_toolsは実際に`SyncTarget.upsert_record()`が書き込み成功を示す値
    （Noneでない戻り値）を返したツール、skipped_toolsはそれ以外（targetが未接続、または
    `SyncTarget.upsert_record()`がNoneを返した＝ツール側の都合で実際には書き込まれなかった
    ケース）を表す。両者は排反であり、written_tools | skipped_tools は「書き込み対象として
    意図したツール」の全体と一致する（shirokuma-sec/obasan-qualityレビュー: 「同期スキップが
    成功として見える」問題への対応）。
    """

    property_name: str
    resolution: ConflictResolution | None  # コンフリクト判定を経由しなかった単純伝播の場合はNone
    written_tools: frozenset[Tool] = field(default_factory=frozenset)
    skipped_tools: frozenset[Tool] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DispatchResult:
    """dispatchの呼び出し結果。"""

    skipped: bool
    reason: str | None = None
    properties: tuple[PropertyDispatchResult, ...] = ()

    @property
    def has_partial_skips(self) -> bool:
        """dispatch自体はskipped=False（処理された）だが、一部プロパティで意図した書き込み先
        ツールのうち実際には書き込めなかったものがある場合にTrueを返す
        （呼び出し側がログ・レスポンスへ反映するための判定に使う）。
        """
        return any(p.skipped_tools for p in self.properties)


class Dispatcher:
    """SyncEventを受け取り、各SyncTargetへの反映を指示するハブ。"""

    def __init__(
        self,
        id_mapping_store: IdMappingStore,
        targets: dict[Tool, SyncTarget],
        *,
        sync_system_id: str | None = None,
        slack_notifier: SlackNotifier | None = None,
    ) -> None:
        self._store = id_mapping_store
        self._targets = targets
        self._sync_system_id = sync_system_id
        # 05_同期・競合制御「アラート通知」。未指定時は通知を送らない
        # （Slack Webhook未設定のローカル開発・テスト環境での動作を妨げないため）。
        self._slack_notifier = slack_notifier

    def dispatch(self, event: SyncEvent) -> DispatchResult:
        # 2. 無限ループ防止：同期エンジン自身の書き込みで発生したWebhookは再処理しない。
        if is_own_system_event(event.sync_system_id, expected=self._sync_system_id):
            return DispatchResult(skipped=True, reason="own_system_event")

        # 3. IDマッピングストアでレコードを特定する。
        mapping = self._resolve_mapping(event)
        if mapping is None:
            # 新規レコード作成フローは本ディスパッチャのスコープ外（後続タスクで対応）。
            return DispatchResult(skipped=True, reason="unknown_record")

        # 差分更新の原則：last_synced_atより新しいイベントのみ処理する。
        if mapping.last_synced_at is not None and event.occurred_at <= mapping.last_synced_at:
            return DispatchResult(skipped=True, reason="stale_event")

        schema = get_schema(event.db_key)

        # 1. Self-Exclusion：送信元ツールを対象リストから除外する。
        target_tools = [t for t in _ALL_TOOLS if t != event.source_tool]

        results: list[PropertyDispatchResult] = []
        for property_name, new_value in event.properties.items():
            try:
                prop = schema.get_property(property_name)
            except KeyError:
                # 送信元ツール（Notion/kintone/Zoho/スプシ）を問わず、スキーマ未定義の
                # プロパティが1件でも含まれるとイベント全体がKeyErrorで失われてしまうため、
                # そのプロパティだけをスキップし他のプロパティの処理は継続する。
                logger.warning(
                    "ignoring unknown property '%s' for db_key=%r (source_tool=%s, not in schema)",
                    property_name,
                    event.db_key,
                    event.source_tool.value,
                )
                continue

            if event.source_tool is Tool.NOTION:
                # Notionは常にマスターであり、Notion発の変更に競合判定は不要。
                # 4. sync_scopeで同期対象と判定されたツールへのみ伝播する。
                intended = frozenset(t for t in target_tools if prop.should_sync_to(t))
                written, skipped = self._write_values(intended, mapping, property_name, new_value)
                results.append(
                    PropertyDispatchResult(
                        property_name=property_name,
                        resolution=None,
                        written_tools=written,
                        skipped_tools=skipped,
                    )
                )
                continue

            # 5. 送信元がNotion以外の場合は、sync_scope対象の全ツールの現在値を集めて
            # コンフリクトの疑いを判定する（BLOCKER3: Notion・送信元の2者間比較のみに
            # 限定しない）。
            other_tools = frozenset(
                t
                for t in _ALL_TOOLS
                if t is not Tool.NOTION and t is not event.source_tool and prop.should_sync_to(t)
            )

            notion_target = self._targets.get(Tool.NOTION)
            notion_record = notion_target.get_record(mapping.notion_key) if notion_target else None

            if notion_record is None:
                # BLOCKER1対応：Notion側の現在値が取得できない（未取得・削除済み・API障害等）
                # 場合、これを「空欄」として扱いコンフリクト判定に持ち込むと、たった今
                # ソース側に保存された値が「空欄化が新しい」と誤判定され全ツールへ
                # Noneで伝播してしまう事故につながる。この場合はコンフリクト判定自体を
                # スキップし、ソース側の値をそのまま各ツールへ最新化する（単純補完）。
                # ここは「Notionページ自体が読めない」場合の話であり、下のNOTION_LAST_EDITED_TIME_KEY
                # によるupdated_at取得（Notionページは読めるが値やタイムスタンプの比較が必要な
                # ケース）とは別物。2026-08本番障害はこちらではなく、後者でupdated_atが常に
                # フォールバックしていたことが原因（conflict_resolver.pyの修正で対応済み）。
                intended = frozenset({Tool.NOTION}) | other_tools
                written, skipped = self._write_values(intended, mapping, property_name, new_value)
                results.append(
                    PropertyDispatchResult(
                        property_name=property_name,
                        resolution=None,
                        written_tools=written,
                        skipped_tools=skipped,
                    )
                )
                continue

            candidates = [
                ToolValue(
                    tool=Tool.NOTION,
                    value=notion_record.get(property_name),
                    # NotionSyncTarget.get_record() -> HttpNotionClient.get_page()が合成する
                    # ページの実際の最終更新日時（NOTION_LAST_EDITED_TIME_KEY）。単純な
                    # "updated_at"キーではないのは、当該キーがproduct/contact DBスキーマの
                    # 実プロパティ名と衝突しうるため（notion_client.py参照）。
                    updated_at=notion_record.get(NOTION_LAST_EDITED_TIME_KEY, event.occurred_at),
                ),
                ToolValue(tool=event.source_tool, value=new_value, updated_at=event.occurred_at),
            ]
            # Notion・送信元以外のsync_scope対象ツールも現在値を取得し比較に加える。
            # 現在値が取得できないツール（レコード未作成等）は「空欄」として候補に含めず、
            # missing_tools として別管理し、確定した値の書き込み対象にのみ含める。
            missing_tools: set[Tool] = set()
            for tool in other_tools:
                target = self._targets.get(tool)
                external_id = _external_id_for(tool, mapping)
                record = (
                    target.get_record(external_id)
                    if target is not None and external_id is not None
                    else None
                )
                if record is None:
                    missing_tools.add(tool)
                    continue
                candidates.append(
                    ToolValue(
                        tool=tool,
                        value=record.get(property_name),
                        updated_at=record.get("updated_at", event.occurred_at),
                    )
                )

            resolution = resolve_conflict(
                mapping.notion_key,
                property_name,
                candidates,
                db_key=event.db_key,
                detected_at=event.occurred_at,
            )

            if resolution.action is ResolutionAction.NO_OP:
                results.append(PropertyDispatchResult(property_name=property_name, resolution=resolution))
                continue

            # 書き込み対象 = resolution.target_tools（現在値を比較できたツールのうち採用値と
            # 異なる方。NOTION_OVERRIDE時は送信元自身の訂正も含む） ∪ missing_tools
            # （比較には参加していないが、確定した値へ補完すべきsync_scope対象ツール）。
            intended = frozenset(resolution.target_tools | missing_tools)
            written, skipped = self._write_values(
                intended, mapping, property_name, resolution.resolved_value
            )

            # BLOCKER2: 却下データの退避（データ退避）とSlackアラート通知（重要項目のみ）。
            if resolution.rejected:
                self._log_rejected(resolution.rejected)
                if resolution.notify_slack and self._slack_notifier is not None:
                    for rejected_item in resolution.rejected:
                        self._slack_notifier.notify_conflict(rejected_item)

            results.append(
                PropertyDispatchResult(
                    property_name=property_name,
                    resolution=resolution,
                    written_tools=written,
                    skipped_tools=skipped,
                )
            )

        self._store.update_last_synced_at(mapping.notion_key, event.occurred_at)
        return DispatchResult(skipped=False, properties=tuple(results))

    def _log_rejected(self, rejected: tuple[RejectedData, ...]) -> None:
        """05_同期・競合制御「データ退避」。却下データをスプレッドシート「同期ログ」タブへ記録する。

        SpreadsheetSyncTargetが未接続（targetsにSPREADSHEETが無い等）の場合は記録できないが、
        却下データはRejectedDataとしてresolution.rejectedに残っているため、呼び出し側（テスト・
        バッチ）で別途拾える。
        """
        target = self._targets.get(Tool.SPREADSHEET)
        if not isinstance(target, SpreadsheetSyncTarget):
            return
        for item in rejected:
            target.append_conflict_log(item)

    def _resolve_mapping(self, event: SyncEvent) -> IdMapping | None:
        if event.source_tool is Tool.NOTION:
            return self._store.get(event.external_id)
        return self._store.find_by_external_id(event.source_tool, event.external_id)

    def _write_values(
        self, tools: frozenset[Tool], mapping: IdMapping, property_name: str, value: object
    ) -> tuple[frozenset[Tool], frozenset[Tool]]:
        """`tools`（書き込み対象として意図した全ツール）へ書き込みを試み、実際に書き込めた
        ツール・書き込めなかった（スキップされた）ツールを返す（両者は排反）。
        """
        written: set[Tool] = set()
        skipped: set[Tool] = set()
        for tool in tools:
            if self._write_value(tool, mapping, property_name, value):
                written.add(tool)
            else:
                skipped.add(tool)
        return frozenset(written), frozenset(skipped)

    def _write_value(self, tool: Tool, mapping: IdMapping, property_name: str, value: object) -> bool:
        """1ツールへの書き込みを試み、実際に書き込めたかどうかを返す。

        `SyncTarget.upsert_record()`の契約（`src/sync_engine/sync_targets/base.py`docstring）
        通り、ツール側の都合で実際にはレコードが作成・更新されなかった場合はNoneが返る
        （例: ZohoSyncTargetのENABLE_ZOHO=False時、`_MultiDb*SyncTarget`
        （`src/sync_engine/production_wiring.py`）が外部IDからdb_keyを解決できなかった時等）。
        そのため戻り値がNoneでないことをもって「書き込み成功」とみなす
        （shirokuma-sec/obasan-qualityレビュー: 「同期スキップが成功として見える」問題への対応。
        targetがそもそも`self._targets`に存在しない場合も同様にFalseを返す）。
        """
        target = self._targets.get(tool)
        if target is None:
            return False
        external_id = _external_id_for(tool, mapping)
        result = target.upsert_record(external_id, {property_name: value})
        return result is not None


def _external_id_for(tool: Tool, mapping: IdMapping) -> str | None:
    if tool is Tool.NOTION:
        return mapping.notion_key
    if tool is Tool.KINTONE:
        return mapping.kintone_id
    if tool is Tool.ZOHO:
        return mapping.zoho_id
    if tool is Tool.SPREADSHEET:
        return str(mapping.spreadsheet_row) if mapping.spreadsheet_row is not None else None
    raise ValueError(f"unknown tool: {tool}")
