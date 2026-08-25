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
import os
import time
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
                    target.get_record(external_id, db_key=mapping.db_key)
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

        raw_record = source_target.get_record(event.external_id, db_key=event.db_key)
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
        result = target.upsert_record(external_id, {property_name: value}, db_key=mapping.db_key)
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
