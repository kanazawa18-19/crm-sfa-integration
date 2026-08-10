"""同期エンジン（Webhook受信によるリアルタイム連携）の本番用ファクトリ。

`src/sync_engine/webhook_handlers/*.py` の各`handler()`/`handler_with_proxy()`は
`Dispatcher`を外部から注入される設計であり、テストではフェイクの`IdMappingStore`・
`SyncTarget`を注入している（`tests/sync_engine/test_dispatcher.py`・
`tests/sync_engine/webhook_handlers/test_*.py`参照）。本モジュールは、実際の
Notion/kintone/Zoho/スプレッドシートAPIクライアントを組み立て、本番用の
`Dispatcher`インスタンスを構築する（`scripts/migrate_data.py`の`build_notion_clients()`
と同様の役割を、常駐プロセス・Webhook受信向けに用意したもの）。

■ 複数DB対応について（設計上の既知の制約と対応方針）
`Dispatcher`（`src/sync_engine/dispatcher.py`）はツール単位で1つの`SyncTarget`しか
保持できない（`dict[Tool, SyncTarget]`）。一方、各ツールクライアント
（`HttpNotionClient`/`HttpKintoneClient`等）・`SyncTarget`実装（`NotionSyncTarget`/
`KintoneSyncTarget`等）は1インスタンスにつき1DB（Notionの`database_id`・kintoneの
アプリ・Zohoのモジュール・スプレッドシートのタブ）に固定される設計であり、Notion 6DB
全体を単一の`Dispatcher`インスタンスで正しく扱うには、本来DB単位のルーティングが必要になる
（例: `NotionSyncTarget.upsert_record()`はNotion APIへ送るプロパティ値の型変換に
自分が構築時に固定されたdb_keyのスキーマを使うため、他DBのプロパティ名を渡すと
`KeyError`になりうる）。

本モジュールでは、DB単位で構築した各`SyncTarget`（またはクライアント）を、
`IdMappingStore`（外部IDからdb_keyを逆引きできる）を使って書き込み時に選択する
薄いルーター（`_MultiDb*SyncTarget`）でラップすることでこれを解決する。ただし
`external_id`が未採番（`None`、＝当該ツールにまだレコードが存在しない新規作成）の場合、
または`external_id`からdb_keyを解決できなかった場合はdb_keyを特定する手掛かりが無い
（Notionの場合は「特定できないDBのスキーマで書き込む」のは誤ったプロパティ変換を招く
リスクがあるため）ため、本ルーターでは書き込みを行わずNone（`SyncTarget.upsert_record()`
の契約上「実際には書き込まれなかった」を表す値）を返す。これを`Dispatcher`側が正しく
「スキップ」として`PropertyDispatchResult.skipped_tools`へ計上する
（shirokuma-sec/obasan-qualityレビュー: 「同期スキップが成功として見える」問題への対応。
`SkipTrackingDispatcher`が更にこれを検知してwarningログを出す）。`Dispatcher.dispatch()`の
通常の伝播フローでは、`IdMapping`が既に存在する（＝移行済みの）レコードに対してのみ
書き込みが発生するため、現状の運用（移行済みレコードへの反映）ではdb_key解決自体は
概ね成功する想定だが、万一解決に失敗した場合でも「サイレントに成功したふりをする」
ことだけは避ける設計とする。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.clients.kintone_client import HttpKintoneClient
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient
from src.sync_engine.clients.zoho_client import HttpZohoClient
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import IdMappingStore, SQLiteIdMappingStore
from src.sync_engine.slack_notifier import WebhookSlackNotifier
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import get_sync_system_id
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
from src.sync_engine.sync_targets.spreadsheet_sync import SpreadsheetSyncTarget
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget, is_zoho_enabled

logger = logging.getLogger(__name__)

# kintoneと同期するDBは取引先マスタ/案件管理/アクション管理の3アプリのみ
# （webhook_handlers/kintone_webhook.pyの_APP_ID_ENV_VARSと同じ対応関係）。
_KINTONE_DB_ENV_SUFFIX: dict[str, str] = {
    "client_master": "CLIENT",
    "project": "PROJECT",
    "action": "ACTION",
}

# Vercelのサーバーレス実行環境はプロジェクトディレクトリ配下が読み取り専用のことが多く、
# 書き込み可能なのは/tmp配下のみ（かつコンテナ間で永続化されない）。本番運用時は
# 本来DynamoDB/Firestore等の永続ストアへ差し替えるべき（id_mapping.pyのdocstring参照）だが、
# 現時点ではSQLite実装のみのため、暫定的に/tmpをデフォルト置き場とする
# （SYNC_ID_MAPPING_DB_PATHで上書き可能）。
_DEFAULT_ID_MAPPING_DB_PATH = "/tmp/sync_id_mapping.db"


class _MultiDbNotionSyncTarget(SyncTarget):
    """db_key単位で構築した`HttpNotionClient`を、書き込み時に`IdMappingStore`で選択するルーター。"""

    tool = Tool.NOTION

    def __init__(
        self, clients_by_db_key: dict[str, HttpNotionClient], id_mapping_store: IdMappingStore
    ) -> None:
        if not clients_by_db_key:
            raise ValueError("clients_by_db_key must not be empty")
        self._clients_by_db_key = clients_by_db_key
        self._store = id_mapping_store
        # get_page/archive_pageはNotion側のプロパティ型スキーマに依存しない（型情報は
        # レスポンスのtypeフィールドから動的に読み取る）ため、任意の1クライアントで良い。
        self._fallback_client = next(iter(clients_by_db_key.values()))

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        return self._fallback_client.get_page(external_id)

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbNotionSyncTarget: 新規Notionページ作成(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        client = self._client_for(external_id)
        if client is None:
            # 誤ったdb_keyのスキーマで書き込む（プロパティ型変換を誤り、Notion APIへ不正な
            # 形式を送ってしまう恐れがある）よりは、書き込まず「スキップ」として報告する方が
            # 安全（shirokuma-sec/obasan-qualityレビュー: 「同期スキップが成功として見える」
            # 問題対応の一環として、以前の「フォールバッククライアントで強行書き込み」から変更）。
            logger.warning(
                "_MultiDbNotionSyncTarget: notion_key=%r のdb_keyを特定できないため、"
                "書き込みをスキップします（誤ったスキーマでの書き込みによるデータ不整合を"
                "避けるため）",
                external_id,
            )
            return None
        client.update_page(external_id, properties)
        return external_id

    def delete_record(self, external_id: str) -> None:
        self._fallback_client.archive_page(external_id)

    def _client_for(self, notion_key: str) -> HttpNotionClient | None:
        mapping = self._store.get(notion_key)
        if mapping is None or mapping.db_key not in self._clients_by_db_key:
            return None
        return self._clients_by_db_key[mapping.db_key]


class _MultiDbKintoneSyncTarget(SyncTarget):
    """db_key単位で構築した`KintoneSyncTarget`を、`IdMappingStore`で選択するルーター。"""

    tool = Tool.KINTONE

    def __init__(
        self, targets_by_db_key: dict[str, KintoneSyncTarget], id_mapping_store: IdMappingStore
    ) -> None:
        self._targets_by_db_key = targets_by_db_key
        self._store = id_mapping_store

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        target = self._resolve(external_id)
        return target.get_record(external_id) if target is not None else None

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbKintoneSyncTarget: 新規kintoneレコード作成(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        target = self._resolve(external_id)
        if target is None:
            logger.warning(
                "_MultiDbKintoneSyncTarget: external_id=%r のdb_keyを特定できないか、当該DB用の"
                "kintoneアプリが未設定のため、書き込みをスキップします",
                external_id,
            )
            return None
        return target.upsert_record(external_id, properties)

    def delete_record(self, external_id: str) -> None:
        target = self._resolve(external_id)
        if target is not None:
            target.delete_record(external_id)

    def _resolve(self, external_id: str) -> KintoneSyncTarget | None:
        mapping = self._store.find_by_external_id(Tool.KINTONE, external_id)
        if mapping is None:
            return None
        return self._targets_by_db_key.get(mapping.db_key)


class _MultiDbZohoSyncTarget(SyncTarget):
    """db_key単位で構築した`ZohoSyncTarget`を、`IdMappingStore`で選択するルーター。"""

    tool = Tool.ZOHO

    def __init__(
        self, targets_by_db_key: dict[str, ZohoSyncTarget], id_mapping_store: IdMappingStore
    ) -> None:
        self._targets_by_db_key = targets_by_db_key
        self._store = id_mapping_store

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        target = self._resolve(external_id)
        return target.get_record(external_id) if target is not None else None

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbZohoSyncTarget: 新規Zohoレコード作成(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        target = self._resolve(external_id)
        if target is None:
            logger.warning(
                "_MultiDbZohoSyncTarget: external_id=%r のdb_keyを特定できないため、書き込みを"
                "スキップします",
                external_id,
            )
            return None
        return target.upsert_record(external_id, properties)

    def delete_record(self, external_id: str) -> None:
        target = self._resolve(external_id)
        if target is not None:
            target.delete_record(external_id)

    def _resolve(self, external_id: str) -> ZohoSyncTarget | None:
        mapping = self._store.find_by_external_id(Tool.ZOHO, external_id)
        if mapping is None:
            return None
        return self._targets_by_db_key.get(mapping.db_key)


class _MultiDbSpreadsheetSyncTarget(SyncTarget):
    """db_key単位で構築した`SpreadsheetSyncTarget`を、`IdMappingStore`で選択するルーター。"""

    tool = Tool.SPREADSHEET

    def __init__(
        self,
        targets_by_db_key: dict[str, SpreadsheetSyncTarget],
        id_mapping_store: IdMappingStore,
    ) -> None:
        self._targets_by_db_key = targets_by_db_key
        self._store = id_mapping_store

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        target = self._resolve(external_id)
        return target.get_record(external_id) if target is not None else None

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbSpreadsheetSyncTarget: 新規行追加(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        target = self._resolve(external_id)
        if target is None:
            logger.warning(
                "_MultiDbSpreadsheetSyncTarget: external_id=%r のdb_keyを特定できないため、"
                "書き込みをスキップします",
                external_id,
            )
            return None
        return target.upsert_record(external_id, properties)

    def delete_record(self, external_id: str) -> None:
        target = self._resolve(external_id)
        if target is not None:
            target.delete_record(external_id)

    def _resolve(self, external_id: str) -> SpreadsheetSyncTarget | None:
        mapping = self._store.find_by_external_id(Tool.SPREADSHEET, external_id)
        if mapping is None:
            return None
        return self._targets_by_db_key.get(mapping.db_key)


_NON_PERSISTENT_PATH_PREFIXES = ("/tmp",)

# shirokuma-secレビュー対応: 起動のたびに毎回同じ警告を大量出力しログを埋もれさせないよう、
# プロセス内で一度だけ出す（reset_production_wiring()でテスト用にリセットできる）。
_persistence_warning_logged = False


def _warn_if_id_mapping_store_not_persistent(path: str) -> None:
    """IDマッピングストアの永続化先がVercel等のサーバーレス環境で消えうる場所
    （`/tmp`配下、または`:memory:`）のままである場合、目立つ警告ログを出す。

    shirokuma-secレビュー: Vercel Python Functionsの実行環境では`/tmp`配下は
    コンテナのコールドスタートのたびに消え、既存の移行済みレコードのIDマッピングごと
    失われる（`Dispatcher._resolve_mapping()`が`unknown_record`として全ての同期を
    スキップするようになる）。SQLiteを永続ストア（Vercel Marketplace経由のNeon Postgres等）
    へ置き換えるまでの安全網として、Webhook購読登録前に気づけるようにする
    （詳細はdocs/id_mapping_persistence_note.md参照）。
    """
    global _persistence_warning_logged
    if _persistence_warning_logged:
        return
    if path == ":memory:" or path.startswith(_NON_PERSISTENT_PATH_PREFIXES):
        _persistence_warning_logged = True
        logger.warning(
            "【危険】IDマッピングストアの永続化先(SYNC_ID_MAPPING_DB_PATH=%r)が、Vercel "
            "Python Functionsの実行環境ではコンテナのコールドスタートのたびに消える場所に "
            "あります。この状態でWebhook購読登録を行うと、新規レコードだけでなく既存の "
            "移行済みレコードの同期も丸ごとスキップされる恐れがあります（Dispatcherが "
            "IDマッピングを解決できずunknown_recordとしてスキップし続けるため）。Webhook "
            "購読登録の前に、必ずSQLiteを永続ストアへ置き換えてください "
            "（詳細はdocs/id_mapping_persistence_note.md参照）。",
            path,
        )


def _reset_persistence_warning_state() -> None:
    """`_warn_if_id_mapping_store_not_persistent`の「1度だけ」状態をクリアする（テスト用）。"""
    global _persistence_warning_logged
    _persistence_warning_logged = False


def build_id_mapping_store(db_path: str | None = None) -> SQLiteIdMappingStore:
    """本番用のIdMappingStoreを構築する。

    `SYNC_ID_MAPPING_DB_PATH`環境変数（未設定時は`/tmp/sync_id_mapping.db`）で
    永続化先を指定する。":memory:"以外を指定した場合、親ディレクトリが無ければ作成する。
    永続化先が非永続な場所（`/tmp`配下・`:memory:`）の場合は警告ログを出す
    （`_warn_if_id_mapping_store_not_persistent`参照）。
    """
    path = db_path or os.environ.get("SYNC_ID_MAPPING_DB_PATH") or _DEFAULT_ID_MAPPING_DB_PATH
    _warn_if_id_mapping_store_not_persistent(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SQLiteIdMappingStore(path)


def build_notion_clients_by_db() -> dict[str, HttpNotionClient]:
    """db_key単位のHttpNotionClientを組み立てる（`notion_database_id`が設定済みのDBのみ）。

    `NOTION_API_KEY`が未設定の場合は空辞書を返す（Notion同期を無効化する）。
    """
    clients: dict[str, HttpNotionClient] = {}
    for schema in ALL_SCHEMAS:
        if schema.notion_database_id is None:
            continue
        try:
            clients[schema.key] = HttpNotionClient(schema.key, schema.notion_database_id)
        except ValueError:
            logger.warning(
                "NOTION_API_KEYが未設定のため、Notion同期ターゲットを構築できません"
                "（Notion向け同期は無効化されます）"
            )
            return {}
    return clients


def build_kintone_targets_by_db() -> dict[str, KintoneSyncTarget]:
    """db_key単位のKintoneSyncTargetを組み立てる（kintone側アプリが存在する3DBのみ）。"""
    domain = os.environ.get("KINTONE_DOMAIN")
    if not domain:
        logger.info("KINTONE_DOMAIN未設定のため、kintone向け同期は無効化されます")
        return {}

    targets: dict[str, KintoneSyncTarget] = {}
    for db_key, suffix in _KINTONE_DB_ENV_SUFFIX.items():
        app_id = os.environ.get(f"KINTONE_APP_ID_{suffix}")
        api_token = os.environ.get(f"KINTONE_API_TOKEN_{suffix}")
        if not app_id or not api_token:
            logger.info(
                "kintone %s 用の環境変数(KINTONE_APP_ID_%s/KINTONE_API_TOKEN_%s)が未設定のため、"
                "当DBのkintone向け同期は無効化されます",
                db_key,
                suffix,
                suffix,
            )
            continue
        client = HttpKintoneClient(domain, api_token=api_token)
        targets[db_key] = KintoneSyncTarget(client, app_id)
    return targets


def build_zoho_targets_by_db() -> dict[str, ZohoSyncTarget]:
    """db_key単位のZohoSyncTargetを組み立てる（`ENABLE_ZOHO=False`時は空辞書）。"""
    if not is_zoho_enabled():
        return {}
    try:
        client = HttpZohoClient()
    except ValueError:
        logger.warning("Zoho認証情報が未設定のため、Zoho向け同期は無効化されます")
        return {}
    return {schema.key: ZohoSyncTarget(client, schema.zoho_api_module) for schema in ALL_SCHEMAS}


def build_spreadsheet_targets_by_db() -> dict[str, SpreadsheetSyncTarget]:
    """db_key単位のSpreadsheetSyncTargetを組み立てる。"""
    try:
        client = HttpSpreadsheetClient()
    except ValueError:
        logger.warning("スプレッドシート認証情報が未設定のため、スプレッドシート向け同期は無効化されます")
        return {}
    return {
        schema.key: SpreadsheetSyncTarget(client, schema.spreadsheet_sheet_name)
        for schema in ALL_SCHEMAS
    }


def build_production_dispatcher(*, id_mapping_store: IdMappingStore | None = None) -> Dispatcher:
    """本番用のDispatcher（4ツール分のSyncTarget＋IdMappingStore）を組み立てる。

    各ツールの認証情報が未設定の場合、当該ツールへの同期は無効化される（そのツールは
    `targets`辞書に含まれず、`Dispatcher`は当該ツールへの書き込みを単にスキップする。
    `Dispatcher._write_value`はtargetが無い場合に何もしないため安全）。
    """
    store = id_mapping_store or build_id_mapping_store()
    targets: dict[Tool, SyncTarget] = {}

    notion_clients = build_notion_clients_by_db()
    if notion_clients:
        targets[Tool.NOTION] = _MultiDbNotionSyncTarget(notion_clients, store)

    kintone_targets = build_kintone_targets_by_db()
    if kintone_targets:
        targets[Tool.KINTONE] = _MultiDbKintoneSyncTarget(kintone_targets, store)

    zoho_targets = build_zoho_targets_by_db()
    if zoho_targets:
        targets[Tool.ZOHO] = _MultiDbZohoSyncTarget(zoho_targets, store)

    spreadsheet_targets = build_spreadsheet_targets_by_db()
    if spreadsheet_targets:
        targets[Tool.SPREADSHEET] = _MultiDbSpreadsheetSyncTarget(spreadsheet_targets, store)

    return Dispatcher(
        store,
        targets,
        sync_system_id=get_sync_system_id(),
        slack_notifier=WebhookSlackNotifier(),
    )


class SkipTrackingDispatcher:
    """`Dispatcher`をラップし、`_MultiDb*SyncTarget`等が実際には書き込めなかった
    （`PropertyDispatchResult.skipped_tools`が非空の）プロパティを検知した際に
    warningログを出す（`.dispatch()`のシグネチャは`Dispatcher`と同じduck-typingで
    `webhook_handlers/*.py`から透過的に使える）。

    `Dispatcher`本体（`src/sync_engine/dispatcher.py`）は本PR以前からの実装・テスト済み
    ロジックのため変更せず、本ラッパーで外側から可観測性を追加する
    （shirokuma-sec/obasan-qualityレビュー: 「同期スキップが成功として見える」問題対応）。
    直近の`dispatch()`結果を`last_result`に保持し、`src/api/app.py`のWebhookルートが
    レスポンスへ反映できるようにする。
    """

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher
        self.last_result: DispatchResult | None = None

    def dispatch(self, event: SyncEvent) -> DispatchResult:
        result = self._dispatcher.dispatch(event)
        self.last_result = result
        if result.has_partial_skips:
            for prop_result in result.properties:
                if not prop_result.skipped_tools:
                    continue
                logger.warning(
                    "sync write partially skipped (書き込み対象と判定されたが実際には反映"
                    "されなかったツールがあります): db_key=%r external_id=%r property=%r "
                    "written_tools=%s skipped_tools=%s (db_keyが解決できなかった、当該DB用の"
                    "認証情報が未設定等が原因の可能性があります。IDマッピングストアの状態・"
                    "各ツールの環境変数設定を確認してください)",
                    event.db_key,
                    event.external_id,
                    prop_result.property_name,
                    sorted(t.value for t in prop_result.written_tools),
                    sorted(t.value for t in prop_result.skipped_tools),
                )
        return result


class ProductionSyncWiring:
    """Webhookルート（`src/api/app.py`）から使い回す、本番用のDispatcher一式。

    `notion_page_client`はNotion Webhookプロキシ層（`notion_webhook.handler_with_proxy`の
    `notion_client`引数）用。ページ全体の再取得（`get_raw_page`）はdb_keyに依存しないため、
    Dispatcherが内部で使うクライアント群のいずれか1つを流用する。

    `dispatcher`は`SkipTrackingDispatcher`でラップされており、部分的な同期スキップが
    発生した場合にwarningログを出す（上記docstring参照）。
    """

    def __init__(self) -> None:
        self.id_mapping_store: IdMappingStore = build_id_mapping_store()
        notion_clients = build_notion_clients_by_db()
        self.notion_page_client: HttpNotionClient | None = (
            next(iter(notion_clients.values())) if notion_clients else None
        )
        self.dispatcher: SkipTrackingDispatcher = SkipTrackingDispatcher(
            build_production_dispatcher(id_mapping_store=self.id_mapping_store)
        )
        # build_production_dispatcher()内で改めてNotionクライアント一式を構築しており
        # 二重にはなるが、Webhook受信のたびに毎回構築するわけではない（モジュールレベルで
        # 1回だけ構築してプロセス内で使い回す。get_production_wiring()参照）ため許容する。


_wiring_singleton: ProductionSyncWiring | None = None


def get_production_wiring() -> ProductionSyncWiring:
    """プロセス内で使い回す本番用配線のシングルトンを返す（未構築なら初回に構築する）。

    `src/api/dashboard_service.py`のモジュールレベルキャッシュと同様、単一プロセスの
    簡易デプロイ想定のためロックは持たない。
    """
    global _wiring_singleton
    if _wiring_singleton is None:
        _wiring_singleton = ProductionSyncWiring()
    return _wiring_singleton


def reset_production_wiring() -> None:
    """モジュールレベルの配線シングルトンをクリアする（テスト用）。

    永続化先の「1度だけ警告」状態（`_warn_if_id_mapping_store_not_persistent`）もあわせて
    リセットする（テストで警告ログの発火を確認できるようにするため）。
    """
    global _wiring_singleton
    if _wiring_singleton is not None and hasattr(_wiring_singleton.id_mapping_store, "close"):
        _wiring_singleton.id_mapping_store.close()
    _wiring_singleton = None
    _reset_persistence_warning_state()


__all__ = [
    "ProductionSyncWiring",
    "SkipTrackingDispatcher",
    "build_id_mapping_store",
    "build_kintone_targets_by_db",
    "build_notion_clients_by_db",
    "build_production_dispatcher",
    "build_spreadsheet_targets_by_db",
    "build_zoho_targets_by_db",
    "get_production_wiring",
    "reset_production_wiring",
]
