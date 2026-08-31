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

import dataclasses
import logging
import os
import time
from dataclasses import dataclass, field

import requests

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.clients._http import ApiError
from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
from src.sync_engine.conflict_resolver import (
    ConflictResolution,
    RejectedData,
    ResolutionAction,
    ToolValue,
    resolve_conflict,
)
from src.sync_engine.id_mapping import DuplicateExternalIdError, IdMapping, IdMappingStore
from src.sync_engine.new_record_builder import build_notion_properties_for_new_record
from src.sync_engine.slack_notifier import SlackNotifier
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import is_own_system_event
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.sync_targets.spreadsheet_sync import SpreadsheetSyncTarget

logger = logging.getLogger(__name__)

_ALL_TOOLS: tuple[Tool, ...] = (Tool.NOTION, Tool.SPREADSHEET, Tool.KINTONE, Tool.ZOHO)

# 新規レコード作成（`AUTO_CREATE_NEW_RECORDS_ENABLED`、2026-08-25、Round2）のガード用環境変数。
# `RELATION_SYNC_ENABLED`（Round1、既存プロパティの更新）とは意図的に別のフラグにする:
# 新規Notionページの作成は「間違えた場合の実害が大きい」（重複ページ・不完全なページの量産
# リスク）ため、独立してON/OFF・段階導入できるようにする（`PROJECT_MIRROR_SYNC_ENABLED`と
# `PROJECT_MIRROR_READ_ENABLED`を分離した設計思想と同じ）。未設定時は既定で無効
# （＝従来通り`unknown_record`としてスキップするだけの挙動を維持する）。
_AUTO_CREATE_NEW_RECORDS_ENV_VAR = "AUTO_CREATE_NEW_RECORDS_ENABLED"

# 新規レコード作成は「kintone/Zoho発の未知レコードに対応するNotionページを新規作成する」
# （Notionは常にマスターであり、Notion自身が「未知」であることを検知しても新規作成する対象が
# 無い。スプレッドシートは今回のスコープ外）というRound2の要件に限定する。
_NEW_RECORD_SOURCE_TOOLS: frozenset[Tool] = frozenset({Tool.KINTONE, Tool.ZOHO})

# `_register_new_record_mapping()`のリトライ回数（初回呼び出しに加えて追加で試みる回数）。
# BLOCKER1対応（2026-08-25）: Notionページ作成後のIdMapping登録が一時的な障害
# （DB接続断・レート制限等）で失敗した場合に備える。
_MAPPING_REGISTRATION_RETRIES = 2

# リトライ間の固定待機時間（秒）。`src/sync_engine/clients/_http.py`の`request_with_retry`が
# 使う指数バックオフとは異なり、ここでは高々2回のリトライのため簡易な固定待機で十分と判断
# （shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-25）。`DuplicateExternalIdError`
# （真の並行作成による恒久的な失敗）の場合はそもそも待機・リトライしない
# （`_register_new_record_mapping()`参照）。
_MAPPING_REGISTRATION_RETRY_BACKOFF_SECONDS = 0.2


def _auto_create_new_records_enabled() -> bool:
    return os.environ.get(_AUTO_CREATE_NEW_RECORDS_ENV_VAR, "").strip().lower() == "true"


def _is_missing_required_value(value: object) -> bool:
    """新規レコード作成時、`RequirementLevel.REQUIRED`なプロパティの値が「実質的に未入力」か
    どうかを判定する（`properties.get(name)`が存在しない場合のNoneも含む）。

    文字列・リストは空/空白のみを「未入力」として扱う。数値0・真偽値Falseはそれ自体が有効な
    値であり未入力ではないため、この関数はNone/空文字列/空リスト以外はFalseを返す。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


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
            # 新規レコード作成（`AUTO_CREATE_NEW_RECORDS_ENABLED`、2026-08-25、Round2）。
            # kintone/Zoho発の未知レコードのみ対象（Round1・上記の理由参照）。未設定時は
            # 既定で無効のため、従来通り`unknown_record`としてスキップする挙動を維持する
            # （このガードにより既存動作には一切影響しない）。
            if (
                event.source_tool in _NEW_RECORD_SOURCE_TOOLS
                and _auto_create_new_records_enabled()
            ):
                return self._try_create_new_record(event)
            return DispatchResult(skipped=True, reason="unknown_record")

        # 差分更新の原則：last_synced_atより新しいイベントのみ処理する。
        if mapping.last_synced_at is not None and event.occurred_at <= mapping.last_synced_at:
            return DispatchResult(skipped=True, reason="stale_event")

        schema = get_schema(event.db_key)

        # 1. Self-Exclusion：送信元ツールを対象リストから除外する。
        target_tools = [t for t in _ALL_TOOLS if t != event.source_tool]

        results: list[PropertyDispatchResult] = []

        # --- フェーズ1: スキーマ解決（外部I/Oを一切行わない） ---
        # 1イベントは複数プロパティを持ちうる。以前はプロパティごとに
        # 「現在値を取得 → 判定 → 書き込み」を回していたため、2つ目のプロパティで現在値の
        # 取得に失敗すると、1つ目は既に他ツールへ書き込み済みという半端な状態が残った
        # （2026-08-28、通知文面で「既に適用済みのプロパティ」を伝える対症療法で凌いでいた）。
        # ここではイベント全体で必要な現在値を先に取り切り、1件も書き込まないうちに失敗を
        # 確定させる（取得フェーズで失敗したら書き込みはゼロ、が保証される）。
        prepared: list[tuple[str, Any, Any]] = []
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
            prepared.append((property_name, prop, new_value))

        if event.source_tool is Tool.NOTION:
            # Notionは常にマスターであり、Notion発の変更に競合判定は不要。現在値の取得が
            # 発生しないため、取得フェーズを挟まずそのまま書き込む。
            for property_name, prop, new_value in prepared:
                # 4. sync_scopeで同期対象と判定されたツールへのみ伝播する。
                intended = frozenset(t for t in target_tools if prop.should_sync_to(t))
                written, skipped, mapping = self._write_values(
                    intended, mapping, property_name, new_value
                )
                results.append(
                    PropertyDispatchResult(
                        property_name=property_name,
                        resolution=None,
                        written_tools=written,
                        skipped_tools=skipped,
                    )
                )
            self._store.update_last_synced_at(mapping.notion_key, event.occurred_at)
            return DispatchResult(skipped=False, properties=tuple(results))

        if not prepared:
            # 実処理の対象プロパティが1つも無い（全てスキーマ未定義だった）場合は、
            # 現在値の取得自体が不要。ここで取得しに行くと、以前は発生しなかったAPI呼び出しと
            # その失敗（＝イベント全体のスキップ）を新たに生んでしまう。
            self._store.update_last_synced_at(mapping.notion_key, event.occurred_at)
            return DispatchResult(skipped=False, properties=())

        # --- フェーズ2: 現在値の取得（ここまでで書き込みは1件も行っていない） ---
        # 5. 送信元がNotion以外の場合は、sync_scope対象の全ツールの現在値を集めて
        # コンフリクトの疑いを判定する（BLOCKER3: Notion・送信元の2者間比較のみに
        # 限定しない）。取得はツール単位に1回で済ませる（以前はプロパティごとに同じ
        # レコードを取り直しており、5プロパティのイベントなら同じAPIを5回叩いていた）。
        # 副次的に、1イベント内の全プロパティが同一スナップショットを見ることになる。
        needed_tools = frozenset(
            t
            for _, prop, _ in prepared
            for t in _ALL_TOOLS
            if t is not Tool.NOTION and t is not event.source_tool and prop.should_sync_to(t)
        )
        # 取得失敗の通知に載せるプロパティ名。取得はイベント単位になったため特定の
        # プロパティに紐づかないが、運用者が「どのレコードのどの更新か」を辿れるよう
        # 先頭のプロパティ名を文脈として渡す。
        context_property_name = prepared[0][0]

        notion_target = self._targets.get(Tool.NOTION)
        try:
            notion_record = (
                notion_target.get_record(mapping.notion_key) if notion_target else None
            )
        except (ApiError, requests.exceptions.RequestException) as exc:
            # 2026-08-27/28本番障害対応の残存リスクへの決着（docs/relation_sync_activation_
            # note.md「2026-08-27の本番障害対応への追加レビュー対応」参照）。ここは
            # `_try_create_new_record()`とは異なり、既に`IdMapping`が存在するレコードへの
            # 通常の更新イベントで、コンフリクト判定・書き込み判断に使う「現在値」を
            # 取得している。取得できないことを「値が空である」かのように扱って処理を
            # 続けると、prop.should_sync_to()の判定やresolve_conflict()の入力が欠けた
            # まま進み、誤った値で他ツールを上書きしてしまう危険がある（`notion_record is
            # None`分岐が警戒しているのと同種のリスクだが、あちらは「Notionページ自体が
            # 読めない」という確定した状態であるのに対し、こちらは「取得できたかどうか
            # 自体が不明」という性質が異なるため、単純補完へは倒さずこの同期イベント
            # 全体の書き込みを中止する）。
            return self._handle_current_value_fetch_failure(
                event,
                mapping,
                tool=Tool.NOTION,
                reason="update_notion_value_fetch_failed",
                exc=exc,
                property_name=context_property_name,
                already_applied_properties=(),
            )

        records_by_tool: dict[Tool, dict[str, Any]] = {}
        # 現在値が取得できないツール（未接続・レコード未作成等）は「空欄」として比較候補には
        # 含めず、確定した値の書き込み対象にのみ含める。
        unavailable_tools: set[Tool] = set()
        for tool in _ALL_TOOLS:
            if tool not in needed_tools:
                continue
            target = self._targets.get(tool)
            external_id = _external_id_for(tool, mapping)
            if target is None or external_id is None:
                # ツールが未接続、またはこのレコードに対する当該ツールの外部IDが
                # まだ無い（未作成）場合は、取得の失敗ではなく「現在値が無い」正常な
                # ケースであり、この同期イベントの中止は不要。
                unavailable_tools.add(tool)
                continue
            try:
                record = target.get_record(external_id, db_key=mapping.db_key)
            except (ApiError, requests.exceptions.RequestException) as exc:
                # 上記Notion現在値取得と同じ理由（取得失敗を「空欄」として扱って
                # 一部のツールを無視したまま処理を続けない）でこの同期イベント全体の
                # 書き込みを中止する。
                return self._handle_current_value_fetch_failure(
                    event,
                    mapping,
                    tool=tool,
                    reason="update_target_value_fetch_failed",
                    exc=exc,
                    property_name=context_property_name,
                    already_applied_properties=(),
                )
            if record is None:
                unavailable_tools.add(tool)
                continue
            records_by_tool[tool] = record

        # --- フェーズ3: 判定と書き込み（ここから先は現在値の取得を行わない） ---
        for property_name, prop, new_value in prepared:
            other_tools = frozenset(t for t in needed_tools if prop.should_sync_to(t))

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
                written, skipped, mapping = self._write_values(
                    intended, mapping, property_name, new_value
                )
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
            missing_tools: set[Tool] = set()
            for tool in _ALL_TOOLS:
                if tool not in other_tools:
                    continue
                record = records_by_tool.get(tool)
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
            written, skipped, mapping = self._write_values(
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

    def _handle_current_value_fetch_failure(
        self,
        event: SyncEvent,
        mapping: IdMapping,
        *,
        tool: Tool,
        reason: str,
        exc: Exception,
        property_name: str,
        already_applied_properties: tuple[str, ...],
    ) -> DispatchResult:
        """既存レコードへの通常の更新イベントで、`tool`の現在値取得（`get_record()`）が
        `ApiError`/`requests.exceptions.RequestException`で失敗した場合の共通処理
        （2026-08-27/28本番障害対応の残存リスクへの決着、`docs/relation_sync_activation_note.md`
        参照）。

        握るのは`_try_create_new_record()`と同じくこの2種類の例外のみで、それ以外
        （`AttributeError`等のプログラミングエラーが疑われる例外）は従来どおり
        呼び出し元へ伝播させる（このメソッド自体はtry/exceptの外で使うヘルパーであり、
        握る例外の種類を絞る判断は各呼び出し箇所のtry/except節が担う）。

        `property_name`（この現在値取得が失敗した時点で処理していたプロパティ）が失敗したら、
        それ以降のプロパティ（`event.properties`のうちまだ処理していない残り）の書き込みは
        一切行わずスキップへ倒す（部分的に取得できた値で`property_name`自体の更新を続ける、
        取得できなかったツールを無視して進める、といった実装はしない）。
        `self._store.update_last_synced_at()`も呼ばないため、次に同じレコードへの新しい
        イベントが届いた際に再度処理される。

        **2026-08-28以降、`dispatch()`の更新経路から呼ばれる場合の`already_applied_properties`は
        常に空**。`dispatch()`を「イベント全体の現在値を取り切ってから書き込みフェーズに入る」
        3フェーズ構成に作り替えたため、取得の失敗時点で書き込みは1件も発生していないことが
        構造的に保証されるようになった（それ以前は複数プロパティのイベントで1つ目が既に
        書き込み済みのまま2つ目以降でこの経路に入ることがあり、通知文面が
        「書き込みは行われていません」と誤って断定しないよう、既に適用済みのプロパティ名を
        渡す対症療法をとっていた）。

        引数と分岐自体は残している。将来プロパティ単位の取得へ戻す変更が入った場合に、
        通知文面だけが嘘になる状態を避けるため（アトミック性そのものは
        `test_dispatch_writes_nothing_when_fetch_fails_on_multi_property_event`が固定している）。
        """
        logger.exception(
            "dispatch: fetching the current value for tool=%s raised an exception while "
            "processing property=%r; aborting this sync event without applying further "
            "updates (db_key=%r, source_tool=%s, external_id=%r, notion_key=%r, "
            "already_applied_properties=%r)",
            tool.value,
            property_name,
            event.db_key,
            event.source_tool.value,
            event.external_id,
            mapping.notion_key,
            already_applied_properties,
        )
        if self._slack_notifier is not None:
            self._slack_notifier.notify_update_skipped(
                db_key=event.db_key,
                source_tool=event.source_tool,
                external_id=event.external_id,
                reason=reason,
                detail=_build_update_skip_detail(
                    tool=tool,
                    property_name=property_name,
                    already_applied_properties=already_applied_properties,
                    exc=exc,
                ),
            )
        return DispatchResult(skipped=True, reason=reason)

    def _resolve_mapping(self, event: SyncEvent) -> IdMapping | None:
        if event.source_tool is Tool.NOTION:
            return self._store.get(event.external_id)
        # 2026-08-14、shirokuma-secレビューBLOCKER対応: db_keyを渡さない検索だと、kintoneの
        # ように外部IDがdb_key（アプリ）単位で独立採番されているツールで、別db_keyの同番号
        # レコードを取り違える事故がありえた（IdMappingStore.find_by_external_id()の
        # docstring参照）。event.db_keyはWebhookハンドラ側で既に確定しているため、ここで
        # 渡して曖昧さを無くす。
        return self._store.find_by_external_id(
            event.source_tool, event.external_id, db_key=event.db_key
        )

    def _try_create_new_record(self, event: SyncEvent) -> DispatchResult:
        """kintone/Zoho発の未知レコード（`IdMapping`が存在しない）に対応するNotionページを
        新規作成する（`AUTO_CREATE_NEW_RECORDS_ENABLED=true`の場合のみ呼ばれる、2026-08-25、
        Round2）。

        Webhookペイロードは変更差分のみのため、新規ページ作成には`event.source_tool`から
        レコード全体データをAPI経由で取得し直す必要がある
        （`src.sync_engine.new_record_builder.build_notion_properties_for_new_record`参照）。
        変換後、対象db_keyの`RequirementLevel.REQUIRED`なプロパティが1つでも欠けている場合は
        不完全なページを作らずスキップする（例: ⑥アクション履歴DBのtitleプロパティが空）。

        「案件」(project)のような、そもそも紐付け先を特定できないリレーションは、
        `KINTONE_FIELD_TRANSFORMS`/`ZOHO_LABEL_FIELD_MAPPINGS`のいずれにもエントリが存在
        しないため、新規作成時も自然に空欄のまま作成される（Round1と同じ方針）。

        ■ 重複作成の防止について（2026-08-25、shirokuma-sec/obasan-qualityレビューBLOCKER
        対応）: `notion_target.upsert_record()`（Notionページ作成）が成功した直後に
        `IdMapping`登録が失敗すると、kintone/Zoho側の自動リトライ・真の並行Webhookで
        `mapping`が依然Noneのまま本メソッドへ再突入し、同じレコードに対応するNotionページが
        重複作成されうる。以下3段構えで対応する:
        1. `notion_target.upsert_record()`を呼ぶ直前にもう一度`_resolve_mapping()`で
           mappingの有無を再確認する（レース窓を縮める。完全な排他制御ではない）。
        2. `notion_target.upsert_record()`自体の呼び出しを`try/except`で囲む（最終レビュー
           BLOCKER対応: サーバーレス環境ではNotion APIへのPOST自体は成功したがレスポンス
           受信前にタイムアウト/接続断/5xxが発生し例外が送出される、という「ページが実際に
           作られたか不明」なケースが現実的に起こりうる。ここを素通りさせると、例外が
           Webhookハンドラの広いexcept節まで伝播して500応答になり、kintone/Zoho側の
           リトライで本メソッドへ再突入し、まさにここで防ごうとしている重複ページ作成が
           保護ロジックを一切経由せずに再現してしまう。そのため例外はここで捕捉し、
           `_handle_uncertain_notion_page_creation()`が「200を返してリトライさせない・
           Slackで人に確認を促す」という安全側の対応を行う。Round1の「完全一致のみ自動
           反映・曖昧なら要確認キューへ」と同じ、自動で危険な推測をしない設計思想）。
        3. `IdMapping`登録は`expected_last_synced_at=None`のCAS（`IdMappingStore.upsert()`の
           「新規作成を期待する場合はNoneを渡す」契約）で行い、`_register_new_record_mapping()`
           が一時的な障害に対しては短い固定待機を挟んで数回リトライする。それでも失敗した
           場合、作成済みのNotionページをアーカイブする補償アクションを試み
           （`_handle_orphaned_notion_page()`）、成否によらずSlackへ孤児ページIDを含む
           明確なアラートを出す（サイレントな500返却でリトライを誘発させない）。
           なお、真の並行実行で両者ともNotionページ作成に成功した場合、`IdMappingStore.upsert()`
           は`db_key`単位で外部ID（kintone_id/zoho_id）の重複を検査するため
           （`DuplicateExternalIdError`、`_register_new_record_mapping()`はこれを検知したら
           リトライせず即座に補償アクションへ進む）、後勝ちの登録は多くの場合失敗し、
           その側のページが補償アクションでアーカイブされる（自己修復）。**ただし、この
           自己修復は`IdMappingStore`の実装に依存する**: `SQLiteIdMappingStore`はDBレベルの
           UNIQUE制約を持つため確実に検知できるが、本番運用で使う`NotionIdMappingStore`は
           自身のdocstringが明記する通りDBレベルの一意制約が無く、ほぼ同時に2つの
           Webhookが処理された場合は両方の事前チェックが「重複なし」と判定してしまい
           `DuplicateExternalIdError`を検知できないレース窓が残る（分散ロック等による解消は
           スコープ外）。
        """
        source_target = self._targets.get(event.source_tool)
        if source_target is None:
            logger.info(
                "new record creation: no SyncTarget configured for source_tool=%s; skipping "
                "(db_key=%r, external_id=%r)",
                event.source_tool.value,
                event.db_key,
                event.external_id,
            )
            return DispatchResult(skipped=True, reason="new_record_source_unavailable")

        try:
            raw_record = source_target.get_record(event.external_id, db_key=event.db_key)
        except (ApiError, requests.exceptions.RequestException) as exc:
            # 2026-08-27本番障害対応: このメソッドは「元レコードが見つからない
            # （get_record()がNoneを返す）」場合しか想定しておらず、「取得そのものが
            # 例外を送出する」場合（kintone/Zoho APIがエラーレスポンスを返した、
            # ネットワーク断・タイムアウトが発生した等）を考慮していなかった。この経路は
            # AUTO_CREATE_NEW_RECORDS_ENABLEDが未設定の間（Round2有効化前）は通らなかった
            # ため露出しなかったが、フラグOFF時は未知レコードを静かにunknown_recordへ
            # スキップしていた（dispatch()参照）のと同じく、「取得に失敗したら従来通り
            # スキップに倒す」のが正しい挙動であり、例外をWebhookハンドラまで伝播させて
            # 500を返す（kintone/Zoho側のリトライを誘発する）べきではない。
            #
            # ■ どの例外を握るか（重要度: 握りすぎるとバグを隠す）: ここで握るのは
            # ApiError（KintoneApiError/ZohoApiError等、外部APIがエラーレスポンスを
            # 返したことを示す）とrequests.exceptions.RequestException（タイムアウト・
            # 接続断等、ネットワーク層の失敗）の2つに意図的に限定する。理由:
            # 1. これらは「元レコードを取得できなかった」という、raw_record is None
            #    （既存のnew_record_source_not_found分岐）と本質的に同じ性質の失敗であり、
            #    握って安全にスキップへ倒してよい。
            # 2. AttributeError/TypeError/KeyErrorのような、コード側のバグ
            #    （_MultiDbKintoneSyncTarget/KintoneSyncTarget/production_wiringの実装
            #    ミス等）に起因する例外まで握ってしまうと、本来500で気づけたはずの
            #    プログラミングエラーが「スキップ」として静かに握りつぶされ、
            #    kintone/Zoho発の新規レコード作成が理由不明のまま機能しなくなる恐れがある。
            #    そのためbare Exceptionでは受けない。
            #    （なお`_handle_uncertain_notion_page_creation()`やzoho_webhook.pyの
            #    「1フィールド単位で失敗を閉じ込める」箇所がbare Exceptionを広く受けて
            #    いるのは、それぞれ「クライアント実装依存で例外型を保証できない外部SDK
            #    呼び出し」「1フィールドだけ壊れても他フィールド・イベント全体は救う」
            #    という別の事情によるものであり、本箇所にそのまま適用すべき理由ではない。
            #    ここは`SyncTarget.get_record()`という単一の、型で契約が決まっている
            #    呼び出しであり、想定される失敗モードを列挙して限定するほうが安全側。）
            logger.exception(
                "new record creation: fetching the source record raised an exception; "
                "skipping (db_key=%r, source_tool=%s, external_id=%r)",
                event.db_key,
                event.source_tool.value,
                event.external_id,
            )
            if self._slack_notifier is not None:
                self._slack_notifier.notify_new_record_issue(
                    db_key=event.db_key,
                    source_tool=event.source_tool,
                    external_id=event.external_id,
                    reason="source_record_fetch_failed",
                    detail=(
                        "新規レコード作成で元レコードの取得に失敗しました"
                        "（外部APIエラーまたはネットワーク障害）。"
                        f" error={exc!r}"
                    ),
                )
            return DispatchResult(skipped=True, reason="new_record_source_fetch_failed")
        if raw_record is None:
            logger.info(
                "new record creation: source record not found; skipping (db_key=%r, "
                "source_tool=%s, external_id=%r)",
                event.db_key,
                event.source_tool.value,
                event.external_id,
            )
            return DispatchResult(skipped=True, reason="new_record_source_not_found")

        properties = build_notion_properties_for_new_record(
            source_tool=event.source_tool,
            db_key=event.db_key,
            external_id=event.external_id,
            raw_record=raw_record,
        )

        schema = get_schema(event.db_key)
        missing_required = [
            prop.name
            for prop in schema.properties
            if prop.is_required and _is_missing_required_value(properties.get(prop.name))
        ]
        if missing_required:
            logger.warning(
                "new record creation: required properties missing after conversion; skipping "
                "to avoid creating an incomplete Notion page (db_key=%r, source_tool=%s, "
                "external_id=%r, missing=%s)",
                event.db_key,
                event.source_tool.value,
                event.external_id,
                missing_required,
            )
            if self._slack_notifier is not None:
                self._slack_notifier.notify_new_record_issue(
                    db_key=event.db_key,
                    source_tool=event.source_tool,
                    external_id=event.external_id,
                    reason="missing_required_properties",
                    detail=f"必須プロパティが不足しているため作成をスキップしました: {missing_required}",
                )
            return DispatchResult(skipped=True, reason="new_record_missing_required_properties")

        notion_target = self._targets.get(Tool.NOTION)
        if notion_target is None:
            logger.info(
                "new record creation: no Notion SyncTarget configured; skipping (db_key=%r, "
                "source_tool=%s, external_id=%r)",
                event.db_key,
                event.source_tool.value,
                event.external_id,
            )
            return DispatchResult(skipped=True, reason="new_record_notion_target_unavailable")

        # BLOCKER1-1: Notionページ作成直前にもう一度mappingの有無を確認し、レース窓を縮める
        # （上記メソッドdocstring参照。完全な排他制御ではないが、素朴な実装より重複作成の
        # 確率を大きく下げる）。
        if self._resolve_mapping(event) is not None:
            logger.info(
                "new record creation: a mapping was found immediately before Notion page "
                "creation (likely created concurrently by another request); skipping to avoid "
                "a duplicate Notion page (db_key=%r, source_tool=%s, external_id=%r)",
                event.db_key,
                event.source_tool.value,
                event.external_id,
            )
            return DispatchResult(skipped=True, reason="new_record_concurrent_creation_detected")

        try:
            new_notion_key = notion_target.upsert_record(None, properties, db_key=event.db_key)
        except Exception as exc:  # noqa: BLE001 (クライアント実装依存の例外を広く受ける)
            self._handle_uncertain_notion_page_creation(event, exc)
            return DispatchResult(skipped=True, reason="new_record_creation_status_unknown")

        if new_notion_key is None:
            logger.warning(
                "new record creation: Notion page creation was skipped by the sync target "
                "(db_key=%r, source_tool=%s, external_id=%r); no id mapping was registered",
                event.db_key,
                event.source_tool.value,
                event.external_id,
            )
            return DispatchResult(skipped=True, reason="new_record_creation_failed")

        registration_error = self._register_new_record_mapping(
            IdMapping(
                notion_key=new_notion_key,
                db_key=event.db_key,
                kintone_id=event.external_id if event.source_tool is Tool.KINTONE else None,
                zoho_id=event.external_id if event.source_tool is Tool.ZOHO else None,
                last_synced_at=event.occurred_at,
            )
        )
        if registration_error is not None:
            self._handle_orphaned_notion_page(event, notion_target, new_notion_key, registration_error)
            return DispatchResult(skipped=True, reason="new_record_mapping_registration_failed")

        logger.info(
            "new record creation: created a new Notion page and registered the id mapping "
            "(db_key=%r, source_tool=%s, external_id=%r, notion_key=%r)",
            event.db_key,
            event.source_tool.value,
            event.external_id,
            new_notion_key,
        )
        if self._slack_notifier is not None:
            self._slack_notifier.notify_new_record_created(
                db_key=event.db_key,
                source_tool=event.source_tool,
                external_id=event.external_id,
                notion_page_id=new_notion_key,
            )
        return DispatchResult(skipped=False)

    def _handle_uncertain_notion_page_creation(self, event: SyncEvent, error: Exception) -> None:
        """Notionページ作成API呼び出し（`notion_target.upsert_record()`）自体が例外を
        送出し、ページが実際に作成されたかどうか不明な場合の処理（最終レビューBLOCKER対応、
        2026-08-25）。

        サーバーレス環境ではNotion APIへのPOST自体は成功したがレスポンス受信前にタイムアウト/
        接続断/5xxが発生し、呼び出し側が例外を受け取るケースが現実的に起こりうる。この例外を
        呼び出し元（Webhookハンドラ）まで伝播させると、広い`except Exception`が500を返し、
        kintone/Zoho側のリトライで`_try_create_new_record()`へ再突入し、まさにこのメソッド群
        全体が防ごうとしている重複ページ作成が保護ロジックを一切経由せずに再現してしまう
        （`IdMapping`が未登録＝`mapping`は依然Noneのまま再送されるため）。

        そのためここで例外を捕捉し、Webhookレスポンスとしては200（受理済み・リトライ不要）を
        返す一方、ページ作成の成否が不明である旨をSlackへ明確に伝え、人による手動確認を促す
        （「自動で危険な推測をするより、人が確認できる形で安全側に倒す」という、Round1の
        「完全一致のみ自動反映・曖昧なら要確認キューへ」と同じ設計思想）。
        """
        logger.error(
            "new record creation: the Notion page creation API call raised an exception; "
            "whether the page was actually created is unknown (db_key=%r, source_tool=%s, "
            "external_id=%r); returning success to the webhook caller to avoid a "
            "retry-induced duplicate page, but manual verification in Notion is required",
            event.db_key,
            event.source_tool.value,
            event.external_id,
            exc_info=error,
        )
        if self._slack_notifier is not None:
            self._slack_notifier.notify_new_record_issue(
                db_key=event.db_key,
                source_tool=event.source_tool,
                external_id=event.external_id,
                reason="notion_creation_status_unknown",
                detail=(
                    "新規レコード作成でNotion API呼び出し自体が失敗し、ページが実際に"
                    "作られたか不明です。手動でNotion側を確認してください"
                    "（重複ページが残っている可能性があります）。"
                    f" error={error!r}"
                ),
            )

    def _mapping_was_actually_registered(self, mapping: IdMapping) -> bool:
        """`IdMappingStore.upsert()`が例外で終わったあと、**実は登録されていた**かを確認する
        （2026-08-28）。

        本番のストアはNotionのDB（`NotionIdMappingStore`）であり、書き込みがサーバー側で
        完了していてもレスポンスが返る前に読み取りタイムアウトすると、こちらは失敗として
        受け取る。そのまま補償アクション（作成済みページのアーカイブ）へ進むと、
        **登録済みのマッピングがアーカイブ済みページを指す**という、放置すると以後の同期が
        壊れる状態を自分で作ってしまう（2026-08-28、external_id=62161で発生）。
        リトライも同様で、既に書き込まれているのに再度書きにいくことになる。

        外部IDから引き直して、**このイベントで作ったページと同じnotion_keyが登録済み**なら
        登録成功とみなす。確認そのものが失敗した場合は`False`（＝従来どおりリトライ・補償へ）。
        推測で成功扱いにはしない。
        """
        if mapping.kintone_id is not None:
            tool, external_id = Tool.KINTONE, mapping.kintone_id
        elif mapping.zoho_id is not None:
            tool, external_id = Tool.ZOHO, mapping.zoho_id
        else:
            return False
        try:
            registered = self._store.find_by_external_id(tool, external_id, db_key=mapping.db_key)
        except Exception:  # noqa: BLE001 (バックエンド実装依存の例外を広く受ける)
            logger.warning(
                "new record creation: could not verify whether the id mapping was actually "
                "registered (notion_key=%r, db_key=%r); falling back to the retry/compensation "
                "path",
                mapping.notion_key,
                mapping.db_key,
                exc_info=True,
            )
            return False
        return registered is not None and registered.notion_key == mapping.notion_key

    def _register_new_record_mapping(self, mapping: IdMapping) -> Exception | None:
        """新規作成したNotionページの`IdMapping`を登録する（BLOCKER1対応、2026-08-25）。

        `expected_last_synced_at=None`のCAS（compare-and-swap）で登録する
        （`IdMappingStore.upsert()`のdocstring「新規作成を期待する場合はNoneを渡す」契約を
        実際に使う）。

        `DuplicateExternalIdError`（外部ID（kintone_id/zoho_id）が`db_key`単位で既に別の
        notion_keyに紐づいている、＝真の並行実行で同じソースレコードから2つのNotionページが
        作られてしまった場合を主に想定）は、リトライしても結果が変わらない恒久的な失敗のため、
        待機・リトライせず即座に諦めて返す（shirokuma-sec/obasan-qualityレビューWARN対応、
        2026-08-25）。それ以外の例外（DB接続断・レート制限等の一時的な障害を想定）は、短い
        固定待機（`_MAPPING_REGISTRATION_RETRY_BACKOFF_SECONDS`）を挟んで数回リトライする。

        成功すれば`None`を、最終的に失敗した場合は最後の例外を返す（呼び出し元が補償
        アクション・アラートを行う）。
        """
        last_error: Exception | None = None
        max_attempts = 1 + _MAPPING_REGISTRATION_RETRIES
        for attempt in range(1, max_attempts + 1):
            try:
                self._store.upsert(mapping, expected_last_synced_at=None)
                return None
            except DuplicateExternalIdError as exc:
                logger.warning(
                    "new record creation: id mapping registration failed permanently due to "
                    "a duplicate external id (likely a true concurrent creation by another "
                    "request, notion_key=%r, db_key=%r); giving up without further retries",
                    mapping.notion_key,
                    mapping.db_key,
                    exc_info=True,
                )
                return exc
            except Exception as exc:  # noqa: BLE001 (バックエンド実装依存の例外を広く受ける)
                last_error = exc
                if self._mapping_was_actually_registered(mapping):
                    # 書き込みはサーバー側で完了しており、レスポンスを受け取れなかっただけ
                    # （読み取りタイムアウト等）。リトライも補償アクションも行わない。
                    logger.warning(
                        "new record creation: the id mapping turned out to be registered "
                        "despite the error (notion_key=%r, db_key=%r); treating it as a "
                        "success and skipping both the retry and the compensating archive",
                        mapping.notion_key,
                        mapping.db_key,
                    )
                    return None
                logger.warning(
                    "new record creation: failed to register id mapping (attempt %d/%d, "
                    "notion_key=%r, db_key=%r); %s",
                    attempt,
                    max_attempts,
                    mapping.notion_key,
                    mapping.db_key,
                    "retrying" if attempt < max_attempts else "giving up",
                    exc_info=True,
                )
                if attempt < max_attempts:
                    time.sleep(_MAPPING_REGISTRATION_RETRY_BACKOFF_SECONDS)
        return last_error

    def _handle_orphaned_notion_page(
        self,
        event: SyncEvent,
        notion_target: SyncTarget,
        notion_page_id: str,
        error: Exception,
    ) -> None:
        """Notionページ作成には成功したが`IdMapping`登録に失敗した場合の後始末
        （BLOCKER1対応、2026-08-25）。

        再送・並行Webhookによるページ重複作成を防ぐため、作成済みページのアーカイブ
        （論理削除）を補償アクションとして試みる。アーカイブ自体に失敗した場合も、
        成否によらず孤児ページのNotion page IDを含む明確なSlackアラートを出し、運用者が
        手動で確認・対処できるようにする（サイレントな500返却でリトライ→再度の重複作成を
        誘発させない設計）。
        """
        archived = False
        try:
            notion_target.delete_record(notion_page_id, db_key=event.db_key)
            archived = True
        except Exception:
            logger.exception(
                "new record creation: failed to archive the orphaned Notion page after id "
                "mapping registration failure (notion_page_id=%r, db_key=%r, source_tool=%s, "
                "external_id=%r)",
                notion_page_id,
                event.db_key,
                event.source_tool.value,
                event.external_id,
            )

        logger.error(
            "new record creation: id mapping registration failed after Notion page creation "
            "succeeded (notion_page_id=%r, db_key=%r, source_tool=%s, external_id=%r, "
            "archived=%s): %s",
            notion_page_id,
            event.db_key,
            event.source_tool.value,
            event.external_id,
            archived,
            error,
            exc_info=error,
        )
        if self._slack_notifier is not None:
            self._slack_notifier.notify_new_record_issue(
                db_key=event.db_key,
                source_tool=event.source_tool,
                external_id=event.external_id,
                reason="mapping_registration_failed",
                detail=(
                    "Notionページ作成後、IdMapping登録に失敗しました。"
                    + (
                        "ページはアーカイブ済みです。"
                        if archived
                        else "⚠️ページのアーカイブにも失敗しました。手動確認が必要です。"
                    )
                    + f" error={error!r}"
                ),
                notion_page_id=notion_page_id,
            )

    def _write_values(
        self, tools: frozenset[Tool], mapping: IdMapping, property_name: str, value: object
    ) -> tuple[frozenset[Tool], frozenset[Tool], IdMapping]:
        """`tools`（書き込み対象として意図した全ツール）へ書き込みを試み、実際に書き込めた
        ツール・書き込めなかった（スキップされた）ツール・**更新後のmapping**を返す
        （書き込めたツールとスキップされたツールは排反）。

        mappingを返すのは、スプレッドシートに行を新規作成したときに採番された行番号を
        呼び出し元へ伝えるため。呼び出し元は必ず返り値で自分のmappingを差し替えること。
        """
        written: set[Tool] = set()
        skipped: set[Tool] = set()
        for tool in tools:
            ok, mapping = self._write_value(tool, mapping, property_name, value)
            if ok:
                written.add(tool)
            else:
                skipped.add(tool)
        return frozenset(written), frozenset(skipped), mapping

    def _write_value(
        self, tool: Tool, mapping: IdMapping, property_name: str, value: object
    ) -> tuple[bool, IdMapping]:
        """1ツールへの書き込みを試み、「実際に書き込めたか」と「更新後のmapping」を返す。

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
            return False, mapping
        external_id = _external_id_for(tool, mapping)
        if tool is Tool.SPREADSHEET and external_id is None:
            # 追記の直前にもう一度ストアを読む。別のWebhookが並行して同じレコードの行を
            # 作っていた場合、ここで拾えれば2行目を作らずに済む
            # （Geminiのレビュー指摘、2026-08-31）。読み直しと追記の間の窓は残るため
            # **完全な排他ではない**。フラグを有効化する前に、レコード単位の直列化か
            # advisory lockを入れること。
            latest = self._store.get(mapping.notion_key)
            if latest is not None and latest.spreadsheet_row is not None:
                mapping = latest
                external_id = _external_id_for(tool, mapping)

        result = target.upsert_record(external_id, {property_name: value}, db_key=mapping.db_key)
        if result is None:
            return False, mapping

        # スプレッドシートに行を新規作成した場合、返ってきた行番号を必ず永続化する。
        # `IdMapping`はfrozenなので差し替えた新しいmappingを呼び出し元へ返し、
        # **同じイベント内の次のプロパティ書き込みが同じ行を更新する**ようにする。
        # ここを怠ると、1レコードにつきプロパティの数だけ行が追記される。
        #
        # kintone/Zoho/Notionを同じ扱いにしていないのは意図的で、これらの新規作成は
        # `_try_create_new_record()`が専用の経路（重複作成のガードとマッピング登録を含む）で
        # 担っているため。ここで二重に登録すると、その保護を迂回することになる。
        if tool is Tool.SPREADSHEET and external_id is None:
            return True, self._register_spreadsheet_row(mapping, result)
        return True, mapping

    def _register_spreadsheet_row(self, mapping: IdMapping, row: str) -> IdMapping:
        """追記されたスプレッドシートの行番号を`IdMapping`へ保存し、更新後のmappingを返す。

        **保存に失敗しても、行番号を持ったmappingを返す。**
        当初は「保存できていないのに行があることにしない」という理由で更新前のmappingを
        返していたが、Gemini・ChatGPTの両方から独立に「それは重複を増やす」と指摘され修正した
        （2026-08-31）。**シートには既に行が物理的に追記されている**ので、
        更新前のmappingを返すと同じイベントの次のプロパティで即座にもう1行追記され、
        1レコードがプロパティの数だけの行に散らばる。
        保存失敗時に残るのは「次回のイベントで1行余分に追記される」可能性だけで、
        こちらの方が明確に被害が小さい。

        例外を送出して同期イベント全体を失敗させる案も出たが採らなかった。
        この時点で他のツール（Notion/kintone/Zoho）への書き込みは既に成功している場合があり、
        ここで中断すると**部分書き込みのまま落ちる**。それを避けるために
        `dispatch()`は「取得フェーズを全部終えてから書く」3フェーズ構成にしてある。
        """
        try:
            row_number = int(row)
        except (TypeError, ValueError):
            logger.warning(
                "spreadsheet row registration: 追記された行番号が数値ではありません "
                "(notion_key=%r, row=%r)",
                mapping.notion_key,
                row,
            )
            return mapping

        # **保存の直前にストアを読み直してから行番号を載せる。**
        # `IdMappingStore.upsert()`は全カラムを上書きするため、古いスナップショットから
        # 組み立てると、その間に別プロセスが更新した`kintone_id`/`zoho_id`を
        # 巻き戻してしまう（lost update。2026-08-31、ChatGPTのレビュー指摘）。
        latest = self._store.get(mapping.notion_key)
        base = latest if latest is not None else mapping
        updated = dataclasses.replace(base, spreadsheet_row=row_number)
        # 一時的な障害（DB接続断・レート制限）で行番号を失うと、次回また追記されて
        # 行が重複する。`_register_new_record_mapping()`と同じ回数だけ再試行する。
        for attempt in range(_MAPPING_REGISTRATION_RETRIES + 1):
            try:
                self._store.upsert(updated)
                return updated
            except Exception:  # noqa: BLE001 - 保存できないこと自体は同期を止める理由にしない
                if attempt < _MAPPING_REGISTRATION_RETRIES:
                    time.sleep(_MAPPING_REGISTRATION_RETRY_BACKOFF_SECONDS)
                    continue
                logger.exception(
                    "spreadsheet row registration: 行番号の保存に%d回失敗しました。"
                    "**シートには既に行が追記済み**なので、行番号を持ったmappingをそのまま返し、"
                    "少なくとも同じイベント内での重複追記は防ぐ "
                    "(notion_key=%r, row=%d)",
                    _MAPPING_REGISTRATION_RETRIES + 1,
                    mapping.notion_key,
                    row_number,
                )
        return updated


def _build_update_skip_detail(
    *,
    tool: Tool,
    property_name: str,
    already_applied_properties: tuple[str, ...],
    exc: Exception,
) -> str:
    """`_handle_current_value_fetch_failure()`のSlack通知本文を組み立てる（BLOCKER1対応、
    2026-08-28）。`already_applied_properties`が空の場合（＝このイベントの最初のプロパティで
    失敗した場合）と非空の場合（＝一部プロパティは既に他ツールへ反映済みのまま中断した場合）を
    文面上はっきり区別し、後者を「書き込みは行われていません」と誤って断定しないようにする
    （プロパティ名は業務上の項目名であり機微情報ではないため通知に含めるが、値そのものは
    含めない）。
    """
    if already_applied_properties:
        applied_list = "、".join(already_applied_properties)
        status = (
            f"この同期イベントには複数のプロパティ変更が含まれており、うち「{applied_list}」は"
            f"既に他ツールへ書き込み済みです。「{property_name}」の処理中に{tool.value}の"
            f"現在値取得に失敗したため、「{property_name}」および未処理の残りのプロパティは"
            "適用されていません（一部のみ適用された状態です）。"
        )
    else:
        status = (
            f"「{property_name}」の処理中に{tool.value}の現在値取得に失敗したため、"
            "この同期イベントは一切適用されていません（書き込みは行われていません）。"
        )
    return f"{status} error={exc!r}"


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
