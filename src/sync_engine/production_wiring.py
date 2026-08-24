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

import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.api.user_directory import NotionUserDirectory
from src.calendar_sync.service import sync_next_action_date_to_calendar
from src.calendar_sync.web_engagement_tool_client import WebEngagementToolCalendarClient
from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.lead_sync.service import sync_contact_to_lead
from src.lead_sync.web_engagement_tool_client import WebEngagementToolLeadSyncClient
from src.project_mirror.sync import sync_project_to_mirror
from src.relation_sync.sync import sync_client_name_to_index
from src.sync_engine.clients._http import (
    HOOK_MAX_RETRIES,
    HOOK_TIMEOUT_SECONDS,
    INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
)
from src.sync_engine.clients.kintone_client import HttpKintoneClient
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient
from src.sync_engine.clients.zoho_client import HttpZohoClient
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import IdMappingStore, SQLiteIdMappingStore
from src.sync_engine.notion_id_mapping import NotionIdMappingStore
from src.sync_engine.slack_notifier import WebhookSlackNotifier
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import get_sync_system_id
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
from src.sync_engine.sync_targets.spreadsheet_sync import SpreadsheetSyncTarget
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget, is_zoho_enabled
from src.sync_engine.webhook_handlers.notion_webhook import NotionPageClient

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

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        # db_key引数は受け取るが使わない（他の_MultiDbXSyncTargetと違い、Notionのpage_idは
        # グローバルに一意なため、外部ID＝external_idだけで衝突なく一意に解決できる。
        # `_client_for()`がnotion_key自体からdb_keyを逆引きする既存の仕組みで十分）。
        return self._fallback_client.get_page(external_id)

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
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

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        self._fallback_client.archive_page(external_id)

    def _client_for(self, notion_key: str) -> HttpNotionClient | None:
        mapping = self._store.get(notion_key)
        if mapping is None or mapping.db_key not in self._clients_by_db_key:
            return None
        return self._clients_by_db_key[mapping.db_key]


class _MultiDbKintoneSyncTarget(SyncTarget):
    """db_key単位で構築した`KintoneSyncTarget`を、呼び出し元が渡す`db_key`で選択するルーター。

    2026-08-14、shirokuma-secレビューBLOCKER対応: 以前は`IdMappingStore.find_by_external_id`
    へexternal_idのみで問い合わせてdb_keyを逆引きしていたが、kintoneのレコード番号はアプリ
    単位で独立採番されているため、この逆引きだけでは別アプリの同番号レコードと取り違える
    事故がありえた（実際にkintone→Notion方向のWebhookを有効化した際に発生）。呼び出し元
    （`Dispatcher._write_value`）は元々`mapping.db_key`を保持しているため、逆引きに頼らず
    直接渡してもらう設計に変更した。`id_mapping_store`は本クラスではもう使わない。
    """

    tool = Tool.KINTONE

    def __init__(self, targets_by_db_key: dict[str, KintoneSyncTarget]) -> None:
        self._targets_by_db_key = targets_by_db_key

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        target = self._resolve(db_key)
        return target.get_record(external_id) if target is not None else None

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbKintoneSyncTarget: 新規kintoneレコード作成(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        target = self._resolve(db_key)
        if target is None:
            logger.warning(
                "_MultiDbKintoneSyncTarget: db_key=%r 用のkintoneアプリが未設定のため、"
                "書き込みをスキップします（external_id=%r）",
                db_key,
                external_id,
            )
            return None
        return target.upsert_record(external_id, properties)

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        target = self._resolve(db_key)
        if target is not None:
            target.delete_record(external_id)

    def _resolve(self, db_key: str | None) -> KintoneSyncTarget | None:
        if db_key is None:
            return None
        return self._targets_by_db_key.get(db_key)


class _MultiDbZohoSyncTarget(SyncTarget):
    """db_key単位で構築した`ZohoSyncTarget`を、呼び出し元が渡す`db_key`で選択するルーター。

    `_MultiDbKintoneSyncTarget`と同じ理由・同じ設計変更（2026-08-14）。
    """

    tool = Tool.ZOHO

    def __init__(self, targets_by_db_key: dict[str, ZohoSyncTarget]) -> None:
        self._targets_by_db_key = targets_by_db_key

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        target = self._resolve(db_key)
        return target.get_record(external_id) if target is not None else None

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbZohoSyncTarget: 新規Zohoレコード作成(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        target = self._resolve(db_key)
        if target is None:
            logger.warning(
                "_MultiDbZohoSyncTarget: db_key=%r 用のZohoモジュールが未設定のため、書き込みを"
                "スキップします（external_id=%r）",
                db_key,
                external_id,
            )
            return None
        return target.upsert_record(external_id, properties)

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        target = self._resolve(db_key)
        if target is not None:
            target.delete_record(external_id)

    def _resolve(self, db_key: str | None) -> ZohoSyncTarget | None:
        if db_key is None:
            return None
        return self._targets_by_db_key.get(db_key)


class _MultiDbSpreadsheetSyncTarget(SyncTarget):
    """db_key単位で構築した`SpreadsheetSyncTarget`を、呼び出し元が渡す`db_key`で選択するルーター。

    `_MultiDbKintoneSyncTarget`と同じ理由・同じ設計変更（2026-08-14）。
    """

    tool = Tool.SPREADSHEET

    def __init__(self, targets_by_db_key: dict[str, SpreadsheetSyncTarget]) -> None:
        self._targets_by_db_key = targets_by_db_key

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        target = self._resolve(db_key)
        return target.get_record(external_id) if target is not None else None

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        if external_id is None:
            logger.warning(
                "_MultiDbSpreadsheetSyncTarget: 新規行追加(external_id未指定)はdb_keyを"
                "特定できないため未サポートです。書き込みをスキップします: properties=%r",
                properties,
            )
            return None
        target = self._resolve(db_key)
        if target is None:
            logger.warning(
                "_MultiDbSpreadsheetSyncTarget: db_key=%r 用のスプレッドシートタブが未設定の"
                "ため、書き込みをスキップします（external_id=%r）",
                db_key,
                external_id,
            )
            return None
        return target.upsert_record(external_id, properties)

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        target = self._resolve(db_key)
        if target is not None:
            target.delete_record(external_id)

    def _resolve(self, db_key: str | None) -> SpreadsheetSyncTarget | None:
        if db_key is None:
            return None
        return self._targets_by_db_key.get(db_key)


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


def build_id_mapping_store(db_path: str | None = None) -> IdMappingStore:
    """本番用のIdMappingStoreを構築する。

    `SYNC_ID_MAPPING_BACKEND`環境変数（未設定時は`"sqlite"`）でバックエンドを選択する。

    - `"sqlite"`（既定）: `SYNC_ID_MAPPING_DB_PATH`環境変数（未設定時は`/tmp/sync_id_mapping.db`）で
      永続化先を指定する。":memory:"以外を指定した場合、親ディレクトリが無ければ作成する。
      永続化先が非永続な場所（`/tmp`配下・`:memory:`）の場合は警告ログを出す
      （`_warn_if_id_mapping_store_not_persistent`参照）。
    - `"notion"`: `NotionIdMappingStore`を構築する（GCP/AWS側の永続DBを契約するまでの暫定
      ブリッジ。Notion自体は永続的なため、`_warn_if_id_mapping_store_not_persistent`の警告は
      対象外。詳細はdocs/id_mapping_persistence_note.md参照）。`db_path`引数はこのバックエンドでは
      使われない。`Dispatcher`のWebhook同期リクエストパス（`_resolve_mapping()`・
      `update_last_synced_at()`）から同期的に呼ばれるため、`max_rate_limit_retries`は
      バルク移行向けの既定値（`DEFAULT_MAX_RATE_LIMIT_RETRIES`）ではなく、
      `src/api/dashboard_service.py`/`src/api/task_service.py`と同じ
      `INTERACTIVE_MAX_RATE_LIMIT_RETRIES`（小さい方）を明示的に渡す。

      注意: このバックエンドを選択すると、返された`IdMappingStore`の呼び出し元
      （`Dispatcher`等）は、`SQLiteIdMappingStore`が送出する契約上の例外
      （`ConflictError`/`DuplicateExternalIdError`/`KeyError`）に加えて、Notion API呼び出し
      失敗に起因する`NotionIdMappingStoreApiError`（タイムアウト・5xx・レート制限枯渇・
      認証失敗等）も受け取りうる。`Dispatcher.dispatch()`はこれを個別にcatchしないため、
      Webhookハンドラ層の広い`except Exception:`まで伝播する。
    """
    backend = os.environ.get("SYNC_ID_MAPPING_BACKEND", "sqlite").strip().lower()
    if backend == "notion":
        return NotionIdMappingStore(max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES)

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
    """db_key単位のZohoSyncTargetを組み立てる（`ENABLE_ZOHO=False`時は空辞書）。

    `ZOHO_ACCOUNTS_BASE_URL`/`ZOHO_API_BASE_URL`環境変数が設定されている場合、
    `HttpZohoClient`へ明示的に渡す（Zohoはアカウントの所属データセンター
    （.com/.eu/.in/.jp/.com.cn/.com.au）ごとにベースURLが異なるため）。未設定の場合は
    キーワード引数自体を渡さず、`HttpZohoClient`側のクラスデフォルト（`.com`）に委ねる
    （本モジュールで`.com`をデフォルト値として重複定義しないため）。
    """
    if not is_zoho_enabled():
        return {}
    zoho_kwargs: dict[str, str] = {}
    accounts_base_url = os.environ.get("ZOHO_ACCOUNTS_BASE_URL")
    if accounts_base_url:
        zoho_kwargs["accounts_base_url"] = accounts_base_url
    api_base_url = os.environ.get("ZOHO_API_BASE_URL")
    if api_base_url:
        zoho_kwargs["api_base_url"] = api_base_url
    try:
        client = HttpZohoClient(**zoho_kwargs)
    except ValueError:
        logger.warning("Zoho認証情報が未設定のため、Zoho向け同期は無効化されます")
        return {}
    return {schema.key: ZohoSyncTarget(client, schema.zoho_api_module) for schema in ALL_SCHEMAS}


def build_spreadsheet_targets_by_db() -> dict[str, SpreadsheetSyncTarget]:
    """db_key単位のSpreadsheetSyncTargetを組み立てる。

    `HttpSpreadsheetClient()`は`SPREADSHEET_ID`未設定時に加え、Google側の認証情報
    （`GOOGLE_SERVICE_ACCOUNT_JSON`/`GOOGLE_ACCESS_TOKEN`）が丸ごと未設定の場合にも
    構築時に`ValueError`を送出する（`HttpSpreadsheetClient.__init__`が構築時に一度
    `get_google_access_token()`を呼びfail-fast検証する）。ここでcatchして空辞書を返し、
    スプレッドシート同期を無効化する。以降のリクエストごとのトークン解決自体は
    `HttpSpreadsheetClient._headers()`が`get_google_access_token()`を都度呼び出す
    （サービスアカウント利用時の自動リフレッシュを活かすため）。
    """
    try:
        client = HttpSpreadsheetClient()
    except ValueError:
        logger.warning("スプレッドシート認証情報が未設定のため、スプレッドシート向け同期は無効化されます")
        return {}
    return {
        schema.key: SpreadsheetSyncTarget(client, schema.spreadsheet_sheet_name)
        for schema in ALL_SCHEMAS
    }


def build_calendar_sync_callable() -> Callable[[Mapping[str, Any], str], Any] | None:
    """`notion_webhook.handler_with_proxy`の`calendar_sync`引数へそのまま渡せる
    `Callable[[Mapping[str, Any], str], Any]`を組み立てる（案件管理DBの「次回アクション日」
    変更をweb-engagement-tool側のGoogle Calendar連携APIへ同期する
    `src.calendar_sync.service.sync_next_action_date_to_calendar`をベースにする）。

    `WebEngagementToolCalendarClient()`は`WEB_ENGAGEMENT_TOOL_URL`/`CALENDAR_SYNC_API_TOKEN`が
    未設定の場合に構築時`ValueError`を送出するため、ここでcatchして`None`を返し、Calendar同期
    を無効化する（`build_spreadsheet_targets_by_db`等、他の任意連携と同じ「未設定なら無効化」
    パターン）。

    `timeout`/`max_retries`はクライアントの既定値（`DEFAULT_TIMEOUT_SECONDS`/
    `DEFAULT_MAX_RETRIES`）ではなく、明示的に`HOOK_TIMEOUT_SECONDS`/`HOOK_MAX_RETRIES`を渡す
    （shirokuma-secレビューWARN対応。このフックは`handler_with_proxy()`の中で
    `dispatcher.dispatch()`の後に同期的に呼ばれるため、既定値のままだと最悪ケースでwebhook
    レスポンスを数十秒遅延させうる。詳細は`_http.py`の該当定数のコメント参照）。
    """
    try:
        calendar_client = WebEngagementToolCalendarClient(
            timeout=HOOK_TIMEOUT_SECONDS, max_retries=HOOK_MAX_RETRIES
        )
    except ValueError:
        logger.info(
            "WEB_ENGAGEMENT_TOOL_URL/CALENDAR_SYNC_API_TOKENが未設定のため、Notion次回アクション日"
            "のGoogle Calendar同期は無効化されます"
        )
        return None
    return functools.partial(sync_next_action_date_to_calendar, calendar_client=calendar_client)


def build_lead_sync_callable(
    notion_client: NotionPageClient | None,
) -> Callable[[Mapping[str, Any], str], Any] | None:
    """`notion_webhook.handler_with_proxy`の`lead_sync`引数へそのまま渡せる
    `Callable[[Mapping[str, Any], str], Any]`を組み立てる（連絡先DBのレコードを
    web-engagement-tool側のLeadシステムへ同期する`src.lead_sync.service.sync_contact_to_lead`
    をベースにする）。

    `notion_client`（`ProductionSyncWiring.notion_page_client`を想定）が`None`の場合
    （`NOTION_API_KEY`未設定でNotion同期自体が無効化されている場合）、会社名解決
    （`sync_contact_to_lead`が「取引先マスター」relationの参照先ページを追加取得するために
    Notion APIを呼ぶ）ができないため、同様に無効化し`None`を返す。

    `WebEngagementToolLeadSyncClient()`は`WEB_ENGAGEMENT_TOOL_URL`/`CRM_SFA_SYNC_API_TOKEN`が
    未設定の場合に構築時`ValueError`を送出するため、ここでcatchして`None`を返す
    （`build_calendar_sync_callable`と同じパターン）。

    `timeout`/`max_retries`は`build_calendar_sync_callable`と同じ理由・値
    （`HOOK_TIMEOUT_SECONDS`/`HOOK_MAX_RETRIES`）を明示的に渡す（shirokuma-secレビューWARN
    対応）。
    """
    if notion_client is None:
        logger.info(
            "NOTION_API_KEYが未設定のため、Notion連絡先DBのLead同期は無効化されます"
        )
        return None
    try:
        lead_sync_client = WebEngagementToolLeadSyncClient(
            timeout=HOOK_TIMEOUT_SECONDS, max_retries=HOOK_MAX_RETRIES
        )
    except ValueError:
        logger.info(
            "WEB_ENGAGEMENT_TOOL_URL/CRM_SFA_SYNC_API_TOKENが未設定のため、Notion連絡先DBのLead"
            "同期は無効化されます"
        )
        return None
    return functools.partial(
        sync_contact_to_lead, notion_client=notion_client, lead_sync_client=lead_sync_client
    )


def build_project_mirror_sync_callable(
    notion_client: NotionPageClient | None,
) -> Callable[[Mapping[str, Any], str], Any] | None:
    """`notion_webhook.handler_with_proxy`の`project_mirror_sync`引数へそのまま渡せる
    `Callable[[Mapping[str, Any], str], Any]`を組み立てる（案件管理DBのPostgresミラー
    （`ProjectMirror`）を更新する`src.project_mirror.sync.sync_project_to_mirror`をベースに
    する、2026-08-17）。

    `PROJECT_MIRROR_SYNC_ENABLED`環境変数（既定`false`）でロールアウトを制御する。無効時は
    `calendar_sync`/`lead_sync`と同じ「未設定なら無効化」パターンに合わせ`None`を返す。
    読み取り元の切り替え（`src/api/dashboard_service.py`の`PROJECT_MIRROR_READ_ENABLED`）とは
    あえて別の環境変数にしている。「書き込み同期は開始したがミラーの内容をまだ信頼しきれ
    ない」検証期間を挟み、読み取り元の切り替えだけを独立して後からON/OFFできるようにする
    ための段階導入設計（ロールアウト手順の詳細はdocs/project_mirror_activation_note.md参照）。

    `notion_client`（`ProductionSyncWiring.notion_page_client`を想定）が`None`の場合
    （`NOTION_API_KEY`未設定でNotion同期自体が無効化されている場合）、
    `sync_project_to_mirror`がページ全体の再取得（`get_raw_page`）に使うNotionクライアントが
    無いため、同様に無効化し`None`を返す。
    """
    if os.environ.get("PROJECT_MIRROR_SYNC_ENABLED", "").strip().lower() != "true":
        return None
    if notion_client is None:
        logger.info(
            "NOTION_API_KEYが未設定のため、案件管理DBのPostgresミラー同期は無効化されます"
        )
        return None
    return functools.partial(
        sync_project_to_mirror,
        notion_client=notion_client,
        user_directory=NotionUserDirectory(max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES),
    )


def build_client_name_index_sync_callable(
    notion_client: NotionPageClient | None,
) -> Callable[[Mapping[str, Any], str], Any] | None:
    """`notion_webhook.handler_with_proxy`の`client_name_index_sync`引数へそのまま渡せる
    `Callable[[Mapping[str, Any], str], Any]`を組み立てる（取引先マスターDBの正規化取引先名→
    Notion page IDインデックス（`ClientNameIndex`）を更新する
    `src.relation_sync.sync.sync_client_name_to_index`をベースにする、2026-08-25）。

    `build_project_mirror_sync_callable`と同じ「未設定なら無効化」パターンを踏襲し、
    `RELATION_SYNC_ENABLED`環境変数（既定`false`）でロールアウトを制御する。`ClientNameIndex`
    にはProjectMirrorのような別個の「読み取り元切り替え」フラグは無い（ダッシュボードが直接
    読む対象ではなく、`src.relation_sync.resolve.resolve_client_master_relation`が内部的に
    検索するだけのテーブルのため）。同関数自体も`RELATION_SYNC_ENABLED`を確認しており
    （`resolve.py`参照）、この`build_*`関数がガードしているのは「書き込み系（Webhook反映・
    夜間reconciliation）を動かすかどうか」のみで、resolve側の「読み取りを試みるかどうか」の
    ガードとは責務が分かれている（shirokuma-sec/obasan-qualityレビューBLOCKER対応、
    詳細はdocs/relation_sync_activation_note.md参照）。

    `notion_client`（`ProductionSyncWiring.notion_page_client`を想定）が`None`の場合
    （`NOTION_API_KEY`未設定でNotion同期自体が無効化されている場合）、
    `sync_client_name_to_index`がページ全体の再取得（`get_raw_page`）に使うNotionクライアントが
    無いため、同様に無効化し`None`を返す。
    """
    if os.environ.get("RELATION_SYNC_ENABLED", "").strip().lower() != "true":
        return None
    if notion_client is None:
        logger.info(
            "NOTION_API_KEYが未設定のため、取引先マスターDBのClientNameIndex同期は無効化されます"
        )
        return None
    return functools.partial(sync_client_name_to_index, notion_client=notion_client)


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
        targets[Tool.KINTONE] = _MultiDbKintoneSyncTarget(kintone_targets)

    zoho_targets = build_zoho_targets_by_db()
    if zoho_targets:
        targets[Tool.ZOHO] = _MultiDbZohoSyncTarget(zoho_targets)

    spreadsheet_targets = build_spreadsheet_targets_by_db()
    if spreadsheet_targets:
        targets[Tool.SPREADSHEET] = _MultiDbSpreadsheetSyncTarget(spreadsheet_targets)

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

    `calendar_sync_callable`/`lead_sync_callable`/`project_mirror_sync_callable`/
    `client_name_index_sync_callable`は、それぞれ`notion_webhook.handler_with_proxy`の
    `calendar_sync`/`lead_sync`/`project_mirror_sync`/`client_name_index_sync`引数へそのまま
    渡せる`Callable[[Mapping[str, Any], str], Any]`
    （`build_calendar_sync_callable`/`build_lead_sync_callable`/
    `build_project_mirror_sync_callable`/`build_client_name_index_sync_callable`参照）。
    対応する連携先の環境変数が未設定の場合は`None`になる（Webhookエンドポイント側は`None`の
    場合当該フックを渡さない想定であり、アプリ起動・Webhookリクエスト処理自体はいずれも
    失敗しない）。
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
        self.calendar_sync_callable: Callable[[Mapping[str, Any], str], Any] | None = (
            build_calendar_sync_callable()
        )
        self.lead_sync_callable: Callable[[Mapping[str, Any], str], Any] | None = (
            build_lead_sync_callable(self.notion_page_client)
        )
        self.project_mirror_sync_callable: Callable[[Mapping[str, Any], str], Any] | None = (
            build_project_mirror_sync_callable(self.notion_page_client)
        )
        self.client_name_index_sync_callable: Callable[[Mapping[str, Any], str], Any] | None = (
            build_client_name_index_sync_callable(self.notion_page_client)
        )


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
    "build_calendar_sync_callable",
    "build_client_name_index_sync_callable",
    "build_id_mapping_store",
    "build_kintone_targets_by_db",
    "build_lead_sync_callable",
    "build_notion_clients_by_db",
    "build_production_dispatcher",
    "build_project_mirror_sync_callable",
    "build_spreadsheet_targets_by_db",
    "build_zoho_targets_by_db",
    "get_production_wiring",
    "reset_production_wiring",
]
