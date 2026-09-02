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
from typing import Any, Container, Mapping, MutableMapping

import requests

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.clients._http import ApiError, ConcurrentModificationError
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
from src.sync_engine.spreadsheet_row_lock import acquire_row_creation_lock
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import is_own_system_event
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.sync_targets.spreadsheet_sync import (
    SpreadsheetSyncTarget,
    drop_relation_properties,
)

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


@dataclass(frozen=True)
class _DecidedWrite:
    """判定が済んで、あとは書くだけになった1プロパティ分（2026-09-01）。

    判定（競合解決）を全プロパティぶん先に済ませ、書き込みは最後にツールごと
    1回にまとめるため、その間これを貯めておく。
    """

    property_name: str
    #: 競合解決の結果。Notion発など、判定を行わなかった場合はNone。
    resolution: "ConflictResolution | None"
    value: Any
    #: このプロパティを書き込むべきツール。
    intended: frozenset[Tool]


def _group_by_tool(
    value_by_property: Mapping[str, Any],
    intended_by_property: Mapping[str, frozenset[Tool]],
) -> dict[Tool, dict[str, Any]]:
    """{プロパティ: 値} と {プロパティ: 送る先ツール} から、{ツール: {プロパティ: 値}} を作る。"""
    payload: dict[Tool, dict[str, Any]] = {}
    for property_name, tools in intended_by_property.items():
        for tool in tools:
            payload.setdefault(tool, {})[property_name] = value_by_property[property_name]
    return payload


def _property_result(
    property_name: str,
    resolution: "ConflictResolution | None",
    intended_by_property: Mapping[str, frozenset[Tool]],
    written_by_tool: Mapping[Tool, frozenset[str]],
) -> "PropertyDispatchResult":
    """ツール単位の書き込み結果を、プロパティ単位の報告へ組み直す。"""
    intended = intended_by_property.get(property_name, frozenset())
    written = frozenset(
        tool for tool in intended if property_name in written_by_tool.get(tool, frozenset())
    )
    return PropertyDispatchResult(
        property_name=property_name,
        resolution=resolution,
        written_tools=written,
        skipped_tools=intended - written,
    )


class _VersionTracker:
    """1イベントの間、書き込みに添える「版」を管理する（2026-09-01）。

    版（kintoneの`$revision`・Zohoの`Modified_Time`）は**こちらが書くたびに相手側で
    進む**。イベントの先頭で読んだ版を同じイベント内で使い回すと、2つ目以降の
    プロパティで「読んだ後に誰かが更新した」と誤判定されて409/412で拒否され、
    **その値は再送されないので恒久的に反映されないまま残る**
    （2026-08-31、shirokuma-sec・ChatGPTが独立に指摘）。

    ■ 今は使われない。将来の保険として残している

    **`_write_values()`が「1イベント・1ツールにつき1回」にまとめたので
    （2026-09-01）、この問題は根本から消えた。** いま`take()`はツールごとに
    1回しか呼ばれず、**取り直す側の分岐には到達しない。**
    1イベント内で同じツールへ2回書く経路を作るときのために残してある。
    その変更を入れるときは、この分岐のテストも必ず足すこと。

    なお、取り直す方式には副作用がある。2回の書き込みの間に**第三者が本当に
    編集していても**、最新の版を持っていくので競合として弾かれない。
    つまり偽の競合は消えるが、真の競合の検知も弱まる。使うときはそこも見ること。

    ■ 競合解決のスナップショットには触らない

    `records_by_tool`は競合解決が参照する「イベント開始時点の現在値」で、
    **1イベント内の全プロパティが同じスナップショットを見る**という性質がある。
    ここから要素を消すと、2つ目以降のプロパティで「現在値が無い」と誤判定され、
    競合判定を経ずに上書きされる。版の管理は必ず別に持つ。
    """

    def __init__(
        self,
        dispatcher: "Dispatcher",
        mapping: IdMapping,
        records_by_tool: Mapping[Tool, Mapping[str, Any]],
    ) -> None:
        self._dispatcher = dispatcher
        self._mapping = mapping
        self._initial = {
            tool: dispatcher._expected_version(tool, record)
            for tool, record in records_by_tool.items()
        }
        self._used: set[Tool] = set()

    def take(self, tool: Tool) -> str | None:
        if tool not in self._used:
            version = self._initial.get(tool)
            if version is not None:
                # 版が取れた時だけ「使った」と覚える。版を持たないツール
                # （Notion・スプレッドシート）で無駄な取り直しをしないため。
                self._used.add(tool)
            return version
        record = self._dispatcher._fetch_one_version(tool, self._mapping)
        return self._dispatcher._expected_version(tool, record) if record is not None else None


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
        #
        # **例外が1つだけある。取り繕わずに書いておく**（2026-09-02、ChatGPTの指摘で修正）。
        # 書き込みの直前に`_spreadsheet_properties_for_new_row()`がNotionを読みに行くことが
        # ある（シートに行がまだ無いレコードだけ）。**この取得に失敗した場合、シートへは
        # 書かないが他ツールへは書く。** つまりここだけは「取得の失敗でも書き込みはゼロ」に
        # なっていない。
        #
        #    なぜ全体を中止しないか
        #    ├ Notionが読めないだけで kintone/Zoho への伝播まで止めると、**差分しか
        #    │ 運ばれないWebhookでは、その変更が二度と届かない**（失われる）
        #    └ 行が無いことは後から取り返せる。`verify_spreadsheet_backfill.py`の
        #      「不足」に出るし、次のイベントでも作り直せる
        #
        # つまり**シートの行作成だけを best-effort に落としている**。黙って落とすのではなく、
        # `skipped_tools`に載せて`SkipTrackingDispatcher`経由でSlackにも上げる。
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
            # Notionは常にマスターであり、Notion発の変更に競合判定は不要。
            #
            # **ただし「マスターだから何を上書きしてもよい」ではない**（2026-08-31）。
            # Webhookは発生順に届かず再送もあるため、こちらが処理を始めてから書き終わるまでの
            # 間に相手側で編集されていると、**古い値で新しい編集を潰す**。そこで競合判定は
            # 行わないまま、**版（kintoneのrevision / ZohoのModified_Time）だけ**を読んで
            # 書き込みに添える。相手側で更新されていれば相手が弾いてくれる
            # （`_write_value`の`ConcurrentModificationError`）。
            versions = self._version_tracker(
                mapping, self._fetch_versions(target_tools, mapping)
            )
            # 4. sync_scopeで同期対象と判定されたツールへのみ伝播する。
            #    **プロパティごとに書かず、ツールごとに1回にまとめる**（`_write_values`参照）。
            intended_by_property = {
                property_name: frozenset(t for t in target_tools if prop.should_sync_to(t))
                for property_name, prop, _new_value in prepared
            }
            payload_by_tool = _group_by_tool(
                {name: value for name, _prop, value in prepared}, intended_by_property
            )
            # 行がまだ無いレコードには、変更された項目だけの行を作らない（2026-09-02）。
            # Notion発の経路はここまで現在値を読んでいないため、必要なときだけ取りに行く。
            # **全項目は追記のときにしか使わない**（更新に使うと無関係な列を巻き戻す）。
            payload_by_tool, new_row_properties = self._spreadsheet_properties_for_new_row(
                payload_by_tool, mapping, notion_record=None, notion_record_fetched=False
            )
            written_by_tool, mapping = self._write_values(
                payload_by_tool, mapping, versions, new_row_properties=new_row_properties
            )
            results = [
                _property_result(property_name, None, intended_by_property, written_by_tool)
                for property_name, _prop, _value in prepared
            ]
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

        # 判定結果をいったん貯めて、書き込みは最後にツールごと1回にまとめる。
        decided: list[_DecidedWrite] = []
        # 書く必要が無いと判定されたプロパティ。報告の並び順を元のプロパティ順に保つため、
        # 逐次appendせずここへ入れておく（2026-09-01、shirokuma-secレビューINFO）。
        no_op_results: dict[str, PropertyDispatchResult] = {}

        # 版は書き込みのたびに進むので、専用に管理する（`_VersionTracker`参照）。
        # **`records_by_tool`はここから一切変更しない**（競合解決が見るスナップショット）。
        versions = self._version_tracker(mapping, records_by_tool)

        # --- フェーズ3: 判定と書き込み（現在値の再取得は版の取り直しだけ） ---
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
                decided.append(
                    _DecidedWrite(
                        property_name=property_name,
                        resolution=None,
                        value=new_value,
                        intended=frozenset({Tool.NOTION}) | other_tools,
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
                no_op_results[property_name] = PropertyDispatchResult(
                    property_name=property_name, resolution=resolution
                )
                continue

            # 書き込み対象 = resolution.target_tools（現在値を比較できたツールのうち採用値と
            # 異なる方。NOTION_OVERRIDE時は送信元自身の訂正も含む） ∪ missing_tools
            # （比較には参加していないが、確定した値へ補完すべきsync_scope対象ツール）。
            decided.append(
                _DecidedWrite(
                    property_name=property_name,
                    resolution=resolution,
                    value=resolution.resolved_value,
                    intended=frozenset(resolution.target_tools | missing_tools),
                )
            )


        # --- フェーズ4: 書き込み（ツールごとに1回だけ） ---
        intended_by_property = {d.property_name: d.intended for d in decided}
        payload_by_tool = _group_by_tool(
            {d.property_name: d.value for d in decided}, intended_by_property
        )
        # 行がまだ無いレコードには、変更された項目だけの行を作らない（2026-09-02）。
        # こちらはフェーズ2で読んだNotionのスナップショットをそのまま使う（取り直さない）。
        # **全項目は追記のときにしか使わない**（更新に使うと無関係な列を巻き戻す）。
        payload_by_tool, new_row_properties = self._spreadsheet_properties_for_new_row(
            payload_by_tool, mapping, notion_record=notion_record, notion_record_fetched=True
        )
        written_by_tool, mapping = self._write_values(
            payload_by_tool, mapping, versions, new_row_properties=new_row_properties
        )
        written_results = {
            item.property_name: _property_result(
                item.property_name, item.resolution, intended_by_property, written_by_tool
            )
            for item in decided
        }
        for property_name, _prop, _value in prepared:
            result = no_op_results.get(property_name) or written_results.get(property_name)
            if result is not None:
                results.append(result)

        # BLOCKER2: 却下データの退避とSlackアラート通知（重要項目のみ）。
        # **書き込みの後で行う**（2026-09-01、shirokuma-secレビューWARN）。
        # 判定の直後に出すと、書き込みが例外で落ちてWebhookが再送されたとき、
        # 実際には書いていない却下通知が二重に飛ぶ。
        for item in decided:
            if item.resolution is None or not item.resolution.rejected:
                continue
            self._log_rejected(item.resolution.rejected)
            if item.resolution.notify_slack and self._slack_notifier is not None:
                for rejected_item in item.resolution.rejected:
                    self._slack_notifier.notify_conflict(rejected_item)

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
            # ルックアップ項目からリレーションを引くのに使う（2026-08-31）。
            id_mapping_store=self._store,
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

        new_mapping = IdMapping(
            notion_key=new_notion_key,
            db_key=event.db_key,
            kintone_id=event.external_id if event.source_tool is Tool.KINTONE else None,
            zoho_id=event.external_id if event.source_tool is Tool.ZOHO else None,
            last_synced_at=event.occurred_at,
        )
        registration_error = self._register_new_record_mapping(new_mapping)
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
        # **ここでシートの行も作る**（2026-09-03）。理由は下のメソッドのdocstring参照。
        self._append_spreadsheet_row_for_created_record(event, new_mapping, properties)
        return DispatchResult(skipped=False)

    def _append_spreadsheet_row_for_created_record(
        self, event: SyncEvent, mapping: IdMapping, properties: dict[str, Any]
    ) -> None:
        """kintone/Zoho発で新しく作ったレコードの行を、その場でシートにも**追記する**
        （2026-09-03）。

        **この経路が抜けていた。** `_try_create_new_record()`はNotionページを作って
        `IdMapping`を登録したら、そこで`DispatchResult(skipped=False)`を返して終わっていた。
        他ツール（シート）へは一度も書かない。

        ```
           kintone で新規レコード
                ▼ Round2
           Notion にページができる ＋ IDマッピングが登録される
                ▼
           ✖ シートの行は作られないまま返る      ← ここ
                ▼
           Notion Webhook も来ない（page.created は購読しておらず、
           同期ボット自身の編集は NOTION_SYNC_BOT_ID で捨てられる）
                ▼
           そのレコードは**次に誰かが更新するまで永久に行が無い**
        ```

        2026-09-02に直した「1列だけの行ができる」（`_spreadsheet_properties_for_new_row()`）は
        **行を作る経路に入った後**の話で、こちらは**その経路に入らない**という別の穴。
        9/2の日中にkintone起点でできた24件が全て行なしだったのはこれが原因
        （`verify_spreadsheet_backfill.py`で実測）。

        ■ 使う値は「今Notionページを作るのに使ったもの」そのもの

        新規作成では`build_notion_properties_for_new_record()`が元レコード全体から
        変換した全項目を持っている。**Notionを読み直す必要はない**（ページはミリ秒前に
        この値で作ったばかり）。シートへ流す項目の選び方は更新経路と同じ規則に従う
        （`_spreadsheet_row_properties()`に一本化。リレーションは必ず落とす）。

        ■ **追記しかしない**（`append_only=True`。2026-09-03、シロクマのBLOCKER対応）

        当初は`_write_spreadsheet_value()`の`properties`にも全項目を渡していた。
        **これは事故になる。**`IdMapping`を登録した瞬間から他のワーカーにも見えるため、
        直後の更新Webhookが先に行を作りうる。そこへ古いスナップショットを流すと、
        相手が書いた新しい値を巻き戻す（作成時「取引先名=仮」→直後に正式名称へ修正、
        という日常的な入力で踏む）。行が既にあるなら**何も書かない**のが正しい。

        ■ 失敗しても Round2 は成功として返す

        Notionページと`IdMapping`は既にできている。ここで例外を投げると
        Webhookが500になり、kintone/Zoho側のリトライで`_try_create_new_record()`へ
        再突入する（`_resolve_mapping()`で止まるが、わざわざ危ない経路を叩く理由が無い）。
        行が無いことは後から取り返せる——`verify_spreadsheet_backfill.py`の「不足」に出るし、
        バックフィルでも次の更新イベントでも作り直せる。**黙って落とさず、Slackへ上げる。**

        **例外は`Exception`で広く受ける**（2026-09-03、シロクマとクマが独立に指摘）。
        当初は`ApiError`と`RequestException`だけに絞っていたが、この先には
        `acquire_row_creation_lock()`→`connect_for_advisory_lock()`という**Postgresへの
        生接続**があり、`psycopg`の例外はどちらにも当たらず素通りしていた。
        「握りすぎるとバグを隠す」という`_try_create_new_record()`冒頭の判断とは前提が違う。
        あちらは**まだ何も作っていない**段階なので500で気づけるが、ここは**もう作り終えた
        後**で、500にしても得られるのはリトライだけ。`SkipTrackingDispatcher`も
        `DispatchResult.properties`しか見ないため、この経路の失敗は拾えない。

        ■ **この割り切りの限界**（2026-09-03、ChatGPTがBLOCKER・Geminiが独立にWARN）

        「2xxを返すのに永続的なリトライを持っていない」ため、**Slackを見落とし、
        そのレコードが二度と編集されなければ、シートには永久に現れない**。
        今回直した不具合を、発生条件だけ変えて残していることになる。

        いま拾える手段は3つで、いずれも自動ではない。

        ```
           verify_spreadsheet_backfill.py   「不足」として必ず出る（人が流す）
           backfill_spreadsheet_rows.py     まとめて作り直す（人が流す）
           そのレコードの次の更新イベント     行が無ければ作られる（いつ来るか不定）
        ```

        本筋の対処は**outbox（未完了の書き込みを永続化して再試行する仕組み）**で、
        それが入れば「例外はACKせず500で返す」でも安全になる——mappingが既にある以上、
        再送は`_try_create_new_record()`へ入らず通常の更新経路を通り、そこで行が作られるため。
        規模がこのイシューの外なので、`~/notes/Dev/crm-sfa-integration.md`のTODOに送った。
        **「直した」と書かないこと。ここは割り切っている。**
        """
        target = self._targets.get(Tool.SPREADSHEET)
        if target is None or not _supports_sync_key(target):
            # 同期キーで追記できないターゲットは、そもそも行を作れない。
            return
        if not _row_creation_allowed(target, mapping.db_key):
            # 行を作らない設定のdb_key。書きにいっても`append_with_sync_key()`が弾く。
            return

        # **ここから下は丸ごと1つのtryで囲む**（2026-09-03、ChatGPTのクロスレビュー指摘）。
        # 当初は`_write_spreadsheet_value()`の呼び出しだけを囲み、`get_schema()`は
        # `(KeyError, ValueError)`限定、`_spreadsheet_row_properties()`は素通しだった。
        # すると「シート・DBの障害はACK、スキーマ側のコードの障害は500」という、
        # docstringが宣言したポリシーと食い違う挙動になる。**作り終えた後は全部ACK**で揃える。
        try:
            schema = get_schema(mapping.db_key)
            row_properties = _spreadsheet_row_properties(properties, schema, mapping.db_key)
        except Exception as exc:  # noqa: BLE001 (上記コメント参照)
            # スキーマが引けない・項目を選べないと、どの列がシート行きか判断できない。
            # **欠けた行を作るくらいなら作らない**（2026-09-02と同じ判断）。
            # 更新経路の同じ見送りは`skipped_tools`経由でSlackへ上がるが、この経路は
            # `DispatchResult.properties`が空なので拾われない。ここで直接上げる
            # （2026-09-03、おばさん指摘）。
            logger.error(
                "new record creation: シートへ流す項目を決められません。"
                "**欠けた行を作らないため、シートには書きません** (db_key=%r, notion_key=%r)",
                mapping.db_key,
                mapping.notion_key,
                exc_info=True,
            )
            self._notify_new_record_row_not_created(
                event,
                mapping,
                f"シートへ流す項目を決められませんでした（エラーの種類: {type(exc).__name__}。"
                "スキーマの設定漏れ・デプロイ不整合が疑われます）",
            )
            return

        if not row_properties:
            # シートへ流せる非リレーション項目が1つも無い。同期キーだけの行になるので作らない。
            # **これは静かな永久欠損になりうる**（2026-09-03、ChatGPT指摘）ので info ではなく
            # warning で出す。Slackまでは上げない——`client_master`のように必ず名前が入るDBでは
            # 起きず、起きるとすれば設定の問題で、`verify_spreadsheet_backfill.py`の
            # 「不足」に必ず現れるため。
            logger.warning(
                "new record creation: シートへ書ける項目が無いため行は作りません。"
                "**このレコードはシートに現れません** (db_key=%r, notion_key=%r)",
                mapping.db_key,
                mapping.notion_key,
            )
            return

        try:
            # 更新後の`mapping`（行番号入り）は捨ててよい。行番号の保存は
            # `_register_spreadsheet_row()`がストアへ直接書くので、ここで受け取った
            # オブジェクトを持ち回る先が無い（このメソッドで新規作成は終わる）。
            written, _ = self._write_spreadsheet_value(
                target,
                mapping,
                row_properties,
                new_row_properties=row_properties,
                append_only=True,
            )
        except Exception as exc:  # noqa: BLE001 (上のdocstring「例外は広く受ける」参照)
            logger.warning(
                "new record creation: シートへの行作成に失敗しました "
                "(db_key=%r, source_tool=%s, external_id=%r, notion_key=%r)",
                mapping.db_key,
                event.source_tool.value,
                event.external_id,
                mapping.notion_key,
                exc_info=True,
            )
            # **例外の中身をSlackへ生で流さない**（2026-09-03、GeminiのINFO指摘）。
            # ここへ来る代表格は`psycopg`の接続エラーで、メッセージに接続先ホストや
            # ユーザー名が載る。CLAUDE.md「認証情報をログ・エラーメッセージに出さない」に
            # 従い、Slackには**種類だけ**を出す（全文はサーバー側のログにある）。
            self._notify_new_record_row_not_created(
                event, mapping, f"エラーの種類: {type(exc).__name__}（詳細は本番ログを参照）"
            )
            return

        if not written:
            # 行作成ロックが取れなかった（別ワーカーが作成中）・ターゲットが書き込みを
            # 見送った等。次のイベントかバックフィルで回収できるが、黙らせない。
            logger.warning(
                "new record creation: シートへの行作成が見送られました "
                "(db_key=%r, source_tool=%s, external_id=%r, notion_key=%r)",
                mapping.db_key,
                event.source_tool.value,
                event.external_id,
                mapping.notion_key,
            )
            self._notify_new_record_row_not_created(
                event,
                mapping,
                "別のワーカーが同じレコードの行を作成中だったか、シート側が書き込みを"
                "見送りました（行作成ロックを取れなかった等）",
            )
            return

        logger.info(
            "new record creation: シートの行も作りました "
            "(db_key=%r, source_tool=%s, external_id=%r, notion_key=%r, 項目数=%d)",
            mapping.db_key,
            event.source_tool.value,
            event.external_id,
            mapping.notion_key,
            len(row_properties),
        )

    def _notify_new_record_row_not_created(
        self, event: SyncEvent, mapping: IdMapping, detail: str
    ) -> None:
        """新規レコードのシート行が作れなかったことをSlackへ上げる。

        **`notion_page_id`は渡さない**（2026-09-03、クマ指摘）。`notify_new_record_issue()`は
        それが渡ると「⚠️ 孤児ページの可能性あり」を無条件で付ける。ここでのページは
        `IdMapping`まで登録済みの**正規のページ**で、孤児ではない。読んだ人が
        `mapping_registration_failed`と同じ対応（アーカイブ）を取ると、そのレコードの
        同期が本当に壊れる。ページIDは本文の中に、注意書き抜きで載せる。
        """
        if self._slack_notifier is None:
            return
        try:
            self._slack_notifier.notify_new_record_issue(
                db_key=mapping.db_key,
                source_tool=event.source_tool,
                external_id=event.external_id,
                reason="spreadsheet_row_not_created",
                detail=(
                    "Notionページ（"
                    f"{mapping.notion_key}"
                    "）は正常に作成・登録できましたが、スプレッドシートの行を"
                    f"作れませんでした。{detail}"
                ),
            )
        except Exception:  # noqa: BLE001 (通知の失敗で新規作成を落とさない)
            logger.warning("new record creation: 行未作成のSlack通知に失敗しました", exc_info=True)

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

    def _spreadsheet_properties_for_new_row(
        self,
        payload_by_tool: dict[Tool, dict[str, Any]],
        mapping: IdMapping,
        *,
        notion_record: Mapping[str, Any] | None,
        notion_record_fetched: bool,
    ) -> tuple[dict[Tool, dict[str, Any]], dict[str, Any] | None]:
        """**行がまだ無いレコードに、変更された項目だけの行を作らない**（2026-09-02）。

        Webhookは差分しか運ばない。行がまだ無いレコードでNotion側の1項目を編集すると、
        `append_with_sync_key()`は**その1列と同期キーだけの行**を追記していた
        （取引先名もkintone IDも空の行）。kintone/Zoho発でも同じことが起きる。
        kintone Webhookはレコード全項目を運ぶが、**Notionと値が一致する項目は競合判定で
        NO_OPになって落ちる**ため、書き込み対象に残るのは結局「変わった項目」だけになる。

        そこでマスターであるNotionから全項目を取り直したものを返す。

        ■ **返した全項目は「追記」にしか使わない**（2026-09-02、シロクマ・クマが独立に指摘）

        当初は`payload_by_tool`の中身をそのまま差し替えていた。**これは事故になる。**
        書き込み経路は追記だけではなく、`_write_spreadsheet_value()`は同期キーで行が
        見つかれば更新に化ける。すると**今回のイベントで触っていない列まで、
        取得時点のNotionスナップショットで上書き**される。

        ```
           行番号が未登録なのに、シートには既に行がある
             （行番号の保存に失敗した後・--skip-id-mapping でバックフィルした後）
           別のワーカーが待っている間に行を作り終えた（ロック取得後の再探索で見つかる）
                ▼
           どちらも「追記のつもりが更新」になる経路。ここへ全項目を持ち込むと、
           相手が書いた新しい値を、こちらが読んだ古い値で巻き戻す
        ```

        そのため戻り値は`payload_by_tool`とは**別の口**にし、`_write_spreadsheet_value()`の
        追記の一手にだけ渡す。更新は今までどおり差分だけを書く。

        ■ 戻り値

        ```
           (payload_by_tool, None)        追記のときも差分をそのまま使う（従来どおり）
           (payload_by_tool, 全項目)      追記のときだけ全項目で書く
           (シートを外したpayload, None)  Notionが読めなかった。**シートには書かない**
        ```

        欠けた行を作るくらいなら、行が無いまま残すほうがよい。
        `verify_spreadsheet_backfill.py`の「不足」に出るし、次のイベントでも作り直せる。
        なおこの見送りは`SkipTrackingDispatcher._unexpected_skips()`が拾い、
        行作成が有効なdb_keyなら**Slackへも上がる**（`production_wiring.py`）。

        ■ 取り直す頻度

        `IdMapping.spreadsheet_row`が未登録の間だけ。**行を1つ作れば登録される**ので、
        通常は1レコードにつき生涯1回。行番号の保存に失敗し続ける間は毎回取りに行くが、
        更新経路が行を引き直した時点で登録し直される（`_register_spreadsheet_row()`）ため、
        その状態も次のイベントで解消する。
        """
        properties = payload_by_tool.get(Tool.SPREADSHEET)
        if not properties:
            return payload_by_tool, None
        if mapping.spreadsheet_row is not None:
            # 行番号が登録済み＝既に行がある。差分だけ書けばよい。
            return payload_by_tool, None

        target = self._targets.get(Tool.SPREADSHEET)
        if target is None or not _supports_sync_key(target):
            # 同期キーで追記できないターゲットは、そもそも行を作れない（行番号が無いため）。
            return payload_by_tool, None
        if not _row_creation_allowed(target, mapping.db_key):
            # そもそも行を作らない設定なら、取り直すだけ無駄（書き込みもスキップされる）。
            return payload_by_tool, None

        notion_target = self._targets.get(Tool.NOTION)
        if notion_target is None:
            # **Notionが未接続の構成では取り直す先が無い。**
            # マスターが居ないので、このイベントの値が唯一の情報源になる。従来どおり
            # 手元の値だけで追記する（＝1列だけの行になりうるが、他に採りようがない）。
            # 本番は必ずNotionを接続しているため、ここへ来るのはテストと部分構成だけ。
            return payload_by_tool, None

        record = notion_record
        if not notion_record_fetched:
            try:
                record = notion_target.get_record(mapping.notion_key)
            except (ApiError, requests.exceptions.RequestException):
                logger.warning(
                    "spreadsheet: 行がまだ無いレコードの全項目をNotionから取得できませんでした。"
                    "**欠けた行を作らないため、今回はシートへ書きません** "
                    "(notion_key=%r, db_key=%r)",
                    mapping.notion_key,
                    mapping.db_key,
                    exc_info=True,
                )
                return _without_spreadsheet(payload_by_tool), None

        if record is None:
            logger.warning(
                "spreadsheet: 行がまだ無いレコードのNotionページが読めませんでした。"
                "**欠けた行を作らないため、今回はシートへ書きません** "
                "(notion_key=%r, db_key=%r)",
                mapping.notion_key,
                mapping.db_key,
            )
            return _without_spreadsheet(payload_by_tool), None

        try:
            schema = get_schema(mapping.db_key)
        except (KeyError, ValueError):
            # **スキーマが引けないときは書かない**（2026-09-02、ChatGPTのクロスレビュー指摘）。
            # 当初は「従来どおり書く」に倒していたが、それは今直したばかりの不具合
            # （1列だけの行）を、設定漏れ・デプロイ不整合のときだけ静かに再発させる。
            # どの項目がシート行きかを判断できない以上、行は作らない方が安全。
            logger.error(
                "spreadsheet: db_key=%r のスキーマが引けません。"
                "**欠けた行を作らないため、今回はシートへ書きません** (notion_key=%r)",
                mapping.db_key,
                mapping.notion_key,
                exc_info=True,
            )
            return _without_spreadsheet(payload_by_tool), None

        # シートへ流す項目だけを埋める（選び方は`_spreadsheet_row_properties()`に一本化。
        # 新規レコード作成の経路と同じ規則を使う）。イベントで既に入っている項目は除く。
        filled = _spreadsheet_row_properties(
            record, schema, mapping.db_key, exclude=properties.keys()
        )
        if not filled:
            # **「Notionが空だった」とは限らない。** 正確には「追加できる非リレーション項目が
            # 1つも無かった」（Notion側も空／全部リレーション／既にイベントに入っていた）。
            # いずれにせよ足せるものが無いので、イベントの値だけで追記する。
            # 実際、本番の取引先マスターには「取引先名しか入っていない」レコードが3,406件ある
            # （2026-09-02 実測）。**それは欠けた行ではなく、そういうレコード。**
            return payload_by_tool, None

        logger.info(
            "spreadsheet: 行がまだ無いので、追記のときだけNotionの現在値で%d項目を補います "
            "(notion_key=%r, db_key=%r)",
            len(filled),
            mapping.notion_key,
            mapping.db_key,
        )
        return payload_by_tool, {**filled, **properties}

    def _write_values(
        self,
        payload_by_tool: Mapping[Tool, dict[str, Any]],
        mapping: IdMapping,
        versions: "_VersionTracker | None" = None,
        *,
        new_row_properties: dict[str, Any] | None = None,
    ) -> tuple[dict[Tool, frozenset[str]], IdMapping]:
        """**1イベント・1ツールにつき1回だけ書き込む**（2026-09-01）。

        以前はプロパティごとにAPIを叩いていた。5項目なら同じレコードへ5回。
        呼び出し回数が無駄なだけでなく、**版（楽観的排他）が書くたびに進むため、
        2つ目以降で偽の競合を起こす**という不具合の温床でもあった
        （2026-08-31、shirokuma-sec・ChatGPTが独立に指摘）。

        まとめて1回にすると戻り値だけでは「どの項目が落ちたか」が分からないので、
        書く前に`SyncTarget.unsupported_properties()`でツールへ聞く
        （既定は「全部送れる」。Notion・スプレッドシートは受け取った値をそのまま書ける）。

        ■ 却下データの記録・Slack通知は判定の時点で行っている

        フェーズ3（判定）で`_log_rejected()`と`notify_conflict()`を呼ぶため、
        このフェーズ4で`ConcurrentModificationError`が出て書き込みを取りやめた場合、
        **「同期ログには採用/却下と残っているが、実際にはその値を書いていない」**
        というズレが理論上ありうる。競合そのものは`skipped_tools`に出るので
        気づけないわけではない。まとめ書き込みで新たに生じた話ではなく、
        判定と書き込みを分けたことで見えやすくなっただけ。

        `new_row_properties`は**シートに行を追記するときだけ**使う、そのまま書ける
        **完成形**（不足分だけではない。`_spreadsheet_properties_for_new_row()`が
        差分を上書きした状態で返す）。更新には使わない。Noneなら従来どおり
        `payload_by_tool`の差分をそのまま追記する。

        戻り値は {ツール: 実際に書けたプロパティ名の集合} と、更新後のmapping。
        """
        written_by_tool: dict[Tool, frozenset[str]] = {}
        for tool in _ALL_TOOLS:
            properties = payload_by_tool.get(tool)
            if not properties:
                continue
            target = self._targets.get(tool)
            if target is None:
                written_by_tool[tool] = frozenset()
                continue

            # 追記に使う全項目も同じ物差しで測る（差分の上位集合なので、まとめて聞く）。
            new_row_values = (
                {**new_row_properties, **properties}
                if tool is Tool.SPREADSHEET and new_row_properties is not None
                else None
            )
            try:
                unsupported = frozenset(
                    target.unsupported_properties(
                        new_row_values if new_row_values is not None else properties,
                        db_key=mapping.db_key,
                    )
                )
            except Exception:  # noqa: BLE001 (ツール実装依存)
                # 聞けなかったからといって同期を止めない。そのまま送って相手の判断に任せる。
                logger.warning(
                    "送れない項目の問い合わせに失敗しました。そのまま送ります (tool=%s)",
                    tool.value,
                    exc_info=True,
                )
                unsupported = frozenset()
            sendable = {
                name: value for name, value in properties.items() if name not in unsupported
            }
            if not sendable:
                written_by_tool[tool] = frozenset()
                continue

            sent: dict[str, Any] | None = (
                {name: value for name, value in new_row_values.items() if name not in unsupported}
                if new_row_values is not None
                else None
            )
            ok, mapping = self._write_value(
                tool,
                mapping,
                sendable,
                versions.take(tool) if versions is not None else None,
                new_row_properties=sent,
            )
            # **報告は「実際に送った項目」に合わせる**（2026-09-02、Gemini・ChatGPTが独立に
            # 指摘）。行を新規作成したときは補完した項目も書いているので、差分だけを
            # 書いたことにすると`written_tools`（APIの応答・ログ）が実態とズレる。
            # なおこの集合は**報告用**で、同期の進み具合を持つ値ではない
            # （それは`IdMapping.last_synced_at`）。ここを広げても書き込み判断は変わらない。
            written_by_tool[tool] = (
                frozenset(sent if sent is not None else sendable) if ok else frozenset()
            )
        return written_by_tool, mapping

    def _version_tracker(
        self, mapping: IdMapping, records_by_tool: Mapping[Tool, Mapping[str, Any]] | None
    ) -> _VersionTracker:
        return _VersionTracker(self, mapping, records_by_tool or {})

    def _fetch_one_version(self, tool: Tool, mapping: IdMapping) -> Mapping[str, Any] | None:
        """1ツールぶんの現在値を取り直す（版を取るためだけ）。

        `_take_expected_version()`が「使った版を捨てたあと」に呼ぶ。
        取れなければNoneを返し、版なしで書く（読めないことを理由に同期を止めない）。

        **`mapping`は引数で受け取る。** Dispatcherはプロセス内で使い回される単一の
        インスタンスなので、インスタンス変数に置くと並行リクエストで混ざる。
        """
        target = self._targets.get(tool)
        external_id = _external_id_for(tool, mapping)
        if target is None or external_id is None:
            return None
        try:
            return target.get_record(external_id, db_key=mapping.db_key)
        except Exception:  # noqa: BLE001 (クライアント実装依存の例外を広く受ける)
            logger.warning(
                "版の取り直しに失敗しました。版なしで書き込みます "
                "(tool=%s, db_key=%r, external_id=%r)",
                tool.value,
                mapping.db_key,
                external_id,
                exc_info=True,
            )
            return None

    def _fetch_versions(
        self, tools: frozenset[Tool], mapping: IdMapping
    ) -> dict[Tool, Mapping[str, Any]]:
        """書き込み先の現在値を、**版を取るためだけに**1レコードずつ読む。

        取得に失敗したツールは版なしで書く（読めないことを理由に同期を止めない。
        版が無ければ従来どおりの上書きになるだけで、これまでより悪くはならない）。
        """
        records: dict[Tool, Mapping[str, Any]] = {}
        for tool in tools:
            target = self._targets.get(tool)
            external_id = _external_id_for(tool, mapping)
            if target is None or external_id is None:
                continue
            try:
                record = target.get_record(external_id, db_key=mapping.db_key)
            except Exception:  # noqa: BLE001 (クライアント実装依存の例外を広く受ける)
                logger.warning(
                    "現在の版を取得できませんでした。版なしで書き込みます "
                    "(tool=%s, db_key=%r, external_id=%r)",
                    tool.value,
                    mapping.db_key,
                    external_id,
                    exc_info=True,
                )
                continue
            if record is not None:
                records[tool] = record
        return records

    @staticmethod
    def _expected_version(tool: Tool, record: Mapping[str, Any] | None) -> str | None:
        """フェーズ2で読んだ現在値から「その時点の版」を取り出す（2026-08-31）。

        これを書き込みへ持っていくと、読んでから書くまでの間に相手側で更新されていた場合に
        相手が弾いてくれる（kintoneは`revision`で409、Zohoは`If-Unmodified-Since`で412）。
        Webhookは発生順に届かず再送もあるため、素朴に書くと**古い値で新しい編集を潰す**。
        """
        if record is None:
            return None
        key = "$revision" if tool is Tool.KINTONE else "Modified_Time"
        raw = record.get(key)
        return str(raw) if raw not in (None, "") else None

    def _write_value(
        self,
        tool: Tool,
        mapping: IdMapping,
        properties: dict[str, Any],
        expected_version: str | None = None,
        *,
        new_row_properties: dict[str, Any] | None = None,
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
        if tool is Tool.SPREADSHEET and _supports_sync_key(target):
            return self._write_spreadsheet_value(
                target, mapping, properties, new_row_properties=new_row_properties
            )

        external_id = _external_id_for(tool, mapping)
        try:
            result = target.upsert_record(
                external_id,
                properties,
                db_key=mapping.db_key,
                expected_version=expected_version,
            )
        except ConcurrentModificationError:
            # 読んでから書くまでの間に相手側で更新されていた。**推測で上書きしない。**
            # 「書けなかった」として返し、部分スキップとして可視化する
            # （既知のズレでなければSlackへも上がる）。
            logger.warning(
                "sync write rejected: %s 側でこちらが読んだ後に更新されていたため上書きしません"
                " (db_key=%r, properties=%r, external_id=%r)",
                tool.value,
                mapping.db_key,
                sorted(properties),
                external_id,
            )
            return False, mapping
        return result is not None, mapping

    def _write_spreadsheet_value(
        self,
        target: Any,
        mapping: IdMapping,
        properties: dict[str, Any],
        *,
        new_row_properties: dict[str, Any] | None = None,
        append_only: bool = False,
    ) -> tuple[bool, IdMapping]:
        """スプレッドシートへの書き込み。**行番号ではなく同期キーで行を確定させる。**

        ■ `append_only`（2026-09-03、シロクマのBLOCKER対応）

        「行が無ければ作る。**あるなら何もしない**」に倒すための口。
        新規レコード作成（`_append_spreadsheet_row_for_created_record()`）だけが使う。

        通常の更新は「差分だけを書く」ので、行が見つかったら更新に化けてよい。
        だが新規作成が持っているのは**作成時点のフルスナップショット**で、これを
        更新に流すと事故になる。

        ```
           W1: 新規作成 …… kintoneから取得（取引先名=「仮」）→ Notionページ作成
                            → IdMapping登録 ★ここから他ワーカーにも見える
           W2: 直後の更新 …… 「取引先名=正式名称」に修正され、行を作る（正式名称）
           W1: 続き …… 同期キーで引くと W2 の行が見つかる
                        → 更新に化けて「仮」で上書き ＝ 正式名称が消える
        ```

        `append_only=True` なら、行が見つかった時点で書かずに成功として返す
        （行はもう存在する＝目的は果たされている）。行番号だけ登録し直す。
        
        行番号を恒久IDとして信用すると、次の2つで壊れる（2026-08-31、
        Gemini・ChatGPTのレビュー指摘）。

        1. **人がシートに行を挿入・削除・並べ替える**と行番号がずれ、別レコードを上書きする
        2. **追記は成功したが行番号をDBに保存する前にプロセスが落ちる**と、次回また追記されて
           重複する（SheetsとPostgresにまたがるので、try/exceptでは解決できない）

        どちらも、シート側に書いたNotionキーから引き直せば直る。
        """
        db_key = mapping.db_key
        sync_key = mapping.notion_key

        # 1. **まずシートに書かれた同期キーで引く。これが正。**
        #    行番号（`IdMapping.spreadsheet_row`）より優先するのは、人が行を挿入・削除・
        #    並べ替えると行番号がずれるため。ここで引ければ、ずれていても正しい行に書ける。
        #    「追記は成功したがDBに保存できなかった」行もここで拾えるので、重複を作らずに済む。
        row = target.find_row_by_sync_key(sync_key, db_key=db_key)
        if row is not None and mapping.spreadsheet_row != row:
            logger.info(
                "spreadsheet: 同期キーで行を引き直しました "
                "(notion_key=%r, 保存されていた行=%r, 実際の行=%d)",
                sync_key,
                mapping.spreadsheet_row,
                row,
            )

        # 2. 見つからず、`IdMapping`が持つ行のキーがまだ空なら、その行を引き継いでキーを埋める。
        #    この仕組みより前に作られた行が対象。**キーが入っている行は1で見つかるはず**なので、
        #    ここへ来る「キーが空の行」は、まだ誰のものでもない行だけ。
        if row is None and mapping.spreadsheet_row is not None:
            if target.row_matches_sync_key(mapping.spreadsheet_row, sync_key, db_key=db_key):
                row = mapping.spreadsheet_row
            else:
                logger.warning(
                    "spreadsheet: 保存されていた行が別のレコードのものになっています。"
                    "上書きせず新しい行を作ります (notion_key=%r, row=%d)",
                    sync_key,
                    mapping.spreadsheet_row,
                )

        if row is not None:
            if append_only:
                return True, self._row_already_exists(mapping, row)
            result = target.update_with_sync_key(str(row), properties, sync_key, db_key=db_key)
            if result is None:
                return False, mapping
            # 引き直した行が`IdMapping`と違うなら、保存し直して次回以降の読み直しを減らす。
            if mapping.spreadsheet_row != row:
                mapping = self._register_spreadsheet_row(mapping, str(row))
            return True, mapping

        # 3. どこにも無ければ新規に追記する（同期キーを必ず一緒に書く）。
        #    **ここだけレコード単位で排他する。**「探す→無い→追記する」の間に別のワーカーが
        #    同じレコードの行を作ると2行できるため。行がある場合（＝更新）はロックを取らない。
        #    1レコードにつき生涯1回しか通らない経路なので、常時の負荷にはならない。
        with acquire_row_creation_lock(db_key, sync_key) as acquired:
            if not acquired:
                # 別のワーカーが作成中。ここで追記すると重複するので見送る。
                # 次の同期イベントで同期キーから引けるため、データは失われない。
                return False, mapping

            # ロックを取ってから、もう一度だけ探す。待っている間に相手が作り終えている。
            row = target.find_row_by_sync_key(sync_key, db_key=db_key)
            if row is not None:
                if append_only:
                    return True, self._row_already_exists(mapping, row)
                result = target.update_with_sync_key(
                    str(row), properties, sync_key, db_key=db_key
                )
                if result is None:
                    return False, mapping
                return True, self._register_spreadsheet_row(mapping, str(row))

            # **ここだけが「行を作る」一手。全項目を使ってよいのはここだけ。**
            # 上の更新経路（同期キーで引けた・ロック中に相手が作り終えた）へ全項目を
            # 持ち込むと、今回触っていない列を古いスナップショットで巻き戻す
            # （2026-09-02、シロクマ・クマが独立に指摘）。
            created = target.append_with_sync_key(
                new_row_properties if new_row_properties is not None else properties,
                sync_key,
                db_key=db_key,
            )

        if created is None:
            return False, mapping
        return True, self._register_spreadsheet_row(mapping, created)

    def _row_already_exists(self, mapping: IdMapping, row: int) -> IdMapping:
        """`append_only` で行が既にあったときの後始末（2026-09-03）。

        **書かない。** 相手が書いた値が、こちらの古いスナップショットより新しいため。
        行番号だけ登録して、次回以降の読み直しを減らす。

        ■ 「行番号の登録で相手のIdMappingを巻き戻すのでは」——**巻き戻らない**

        Gemini がここをBLOCKERとして指摘したが（2026-09-03）、実コードで確かめて棄却した。
        `_register_spreadsheet_row()`は**保存の直前にストアを読み直し**、そこへ行番号だけを
        載せる（`latest = self._store.get(...)`）。呼び出し元が握っている古い`mapping`が
        そのまま書かれることはない。この lost update は2026-08-31にChatGPTの指摘で
        既に塞いである。**言われたまま直さない。**
        """
        logger.info(
            "spreadsheet: 行は既にあるので追記しません（別の経路が先に作りました）"
            " (notion_key=%r, db_key=%r, row=%d)",
            mapping.notion_key,
            mapping.db_key,
            row,
        )
        if mapping.spreadsheet_row == row:
            return mapping
        return self._register_spreadsheet_row(mapping, str(row))

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


def _spreadsheet_row_properties(
    source: Mapping[str, Any],
    schema: Any,
    db_key: str,
    *,
    exclude: Container[str] = (),
) -> dict[str, Any]:
    """`source`から**シートの1行に流してよい項目だけ**を取り出す（2026-09-03に一本化）。

    「シートへ流してよい項目とは何か」は業務ルールで、2箇所に散らすと片方だけ直して
    片方を忘れる（2026-09-03、おばさん指摘）。使うのは次の2つ。

    ```
       _spreadsheet_properties_for_new_row()          既にあるレコードの行を作るとき
         source=Notionページの現在値 / exclude=イベントで既に入っている項目

       _append_spreadsheet_row_for_created_record()   新規作成でその場の行を作るとき
         source=Notionページを作るのに使った全項目 / exclude=なし
    ```

    落とすものは2種類。

    1. **スキーマがシートへ同期しない項目**（`properties_synced_to`）。Notionにしか無い
       メタ情報やスキーマ外のプロパティを持ち込まない。`source`に無いキーも飛ばす。
       落ちるのはロールアップ・unique_idのような読み取り専用型だけで、それらは
       `SyncScope.INTERNAL`固定のため最初からこの一覧に入らない（`db_schema/base.py`）。
    2. **リレーション**（`drop_relation_properties`）。書き込み側と同じ関数を使う。
       落とされるものを数に入れると、実際には1列だけの行なのに「補えた」と誤認する
       （2026-09-02、クマ指摘）。

    `exclude`は**プロパティ名の集合**（`Container[str]`）。辞書を渡してもキーで判定
    されるが、型で意図を示しておく（2026-09-03、GeminiのINFO指摘）。
    """
    return drop_relation_properties(
        {
            prop.name: source[prop.name]
            for prop in schema.properties_synced_to(Tool.SPREADSHEET)
            if prop.name not in exclude and prop.name in source
        },
        db_key,
    )


def _without_spreadsheet(
    payload_by_tool: dict[Tool, dict[str, Any]],
) -> dict[Tool, dict[str, Any]]:
    """シートへの書き込みだけを外したペイロードを返す（他ツールへの同期は続ける）。"""
    return {
        tool: values for tool, values in payload_by_tool.items() if tool is not Tool.SPREADSHEET
    }


def _row_creation_allowed(target: object, db_key: str) -> bool:
    """このdb_keyでシートの行を新規作成してよいかを、ターゲット自身に聞く。

    「全項目を取り直すかどうか」の判断にしか使わない。答えられないターゲット
    （テストの単純なFake）は許可扱いにしてよい。**許可の実体は
    `append_with_sync_key()`側が最終的に握る**ので、ここで甘く見ても行は増えない。
    """
    ask = getattr(target, "row_creation_enabled", None)
    if not callable(ask):
        return True
    try:
        return bool(ask(db_key))
    except Exception:  # noqa: BLE001 (ツール実装依存)
        logger.warning(
            "spreadsheet: 行作成の可否を問い合わせられませんでした。許可扱いで続けます "
            "(db_key=%r)",
            db_key,
            exc_info=True,
        )
        return True


def _supports_sync_key(target: object) -> bool:
    """同期キーで行を解決できるスプレッドシートターゲットか。

    テストの単純なFakeターゲットは対応していないため、能力の有無で分岐する
    （対応していない場合は従来どおり行番号だけで書く）。
    """
    return all(
        hasattr(target, name)
        for name in (
            "row_matches_sync_key",
            "find_row_by_sync_key",
            "append_with_sync_key",
            "update_with_sync_key",
        )
    )


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
