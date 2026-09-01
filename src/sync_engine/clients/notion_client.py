"""Notion API (`https://api.notion.com/v1/`) へ実HTTP通信を行う `NotionClient` Protocol実装。

`src/sync_engine/sync_targets/notion_sync.py` の `NotionClient` Protocolを満たす。
1インスタンス = 1 Notion database（`database_id`）に対応する
（`KintoneSyncTarget`/`ZohoSyncTarget`/`SpreadsheetSyncTarget` がDB単位でインスタンス化されるのと同様）。

内部の`properties: dict[str, Any]`（プロパティ名→生の値）とNotion APIが要求する
プロパティ型ごとの形式との相互変換は、`src/db_schema/registry.py`のスキーマ定義
（`PropertyType`）を参照して行う。Notion形式→内部値の変換は
`webhook_handlers/notion_webhook.py`の`parse_notion_property_value`を再利用し、
本モジュールはその逆方向（内部値→Notion形式）のみを追加で実装する。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.audit_log.actor_context import get_actor
from src.audit_log.recorder import record_notion_write
from src.db_schema.base import DatabaseSchema, PropertyType
from src.db_schema.registry import get_schema
from src.sync_engine.clients._notion_paging import query_all_with_keyset
from src.sync_engine.clients.notion_display_resolver import resolve_display_values
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RATE_LIMIT_RETRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    extract_error_message,
    raise_for_error,
    request_with_retry,
)
from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
from src.sync_engine.webhook_handlers._common import parse_iso_datetime
from src.sync_engine.webhook_handlers.notion_webhook import (
    PARSEABLE_NOTION_PROPERTY_TYPES,
    parse_notion_property_value,
)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"

logger = logging.getLogger(__name__)

# get_page()は同期の書き込み対象かどうかに関わらず「今ページに存在する値」を読むだけの
# 用途（マージ判定用の現在値取得）なので、formula/rollup/files/created_time等の
# `PARSEABLE_NOTION_PROPERTY_TYPES`（parse_notion_property_value()が実際に対応する
# Notion APIの生の型文字列の一覧。定義・重複防止の経緯は`notion_webhook.py`側の
# コメントを参照）に無い未対応型は例外にせず読み飛ばす。
#
# NOTION_LAST_EDITED_TIME_KEY（get_page()がページの実際の最終更新日時を合成する際に
# 使う予約キー）の定義・衝突回避の理由は`clients/_notion_keys.py`のコメントを参照。


class NotionApiError(ApiError):
    """Notion API呼び出し失敗時に送出する例外。"""


def _as_id_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _rich_text_content(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    return [{"type": "text", "text": {"content": str(value)}}]


def build_notion_property_value(property_type: PropertyType, value: Any) -> dict[str, Any]:
    """内部値1件を、指定されたPropertyTypeに応じたNotion APIのプロパティ値形式へ変換する。

    `parse_notion_property_value`（Notion形式→内部値）の逆方向にあたる。
    """
    if property_type == PropertyType.TITLE:
        return {"title": _rich_text_content(value)}
    if property_type == PropertyType.TEXT:
        return {"rich_text": _rich_text_content(value)}
    if property_type == PropertyType.SELECT:
        return {"select": ({"name": value} if value else None)}
    if property_type == PropertyType.STATUS:
        return {"status": ({"name": value} if value else None)}
    if property_type == PropertyType.MULTI_SELECT:
        return {"multi_select": [{"name": option} for option in _as_id_list(value)]}
    if property_type in (PropertyType.NUMBER, PropertyType.CURRENCY):
        return {"number": value}
    if property_type in (PropertyType.DATE, PropertyType.DATETIME):
        return {"date": ({"start": value} if value else None)}
    if property_type == PropertyType.EMAIL:
        return {"email": value}
    if property_type == PropertyType.PHONE:
        return {"phone_number": value}
    if property_type == PropertyType.URL:
        return {"url": value}
    if property_type == PropertyType.CHECKBOX:
        return {"checkbox": bool(value)}
    if property_type == PropertyType.USER:
        return {"people": [{"id": user_id} for user_id in _as_id_list(value)]}
    if property_type == PropertyType.RELATION:
        return {"relation": [{"id": related_id} for related_id in _as_id_list(value)]}
    if property_type == PropertyType.JSON_TEXT:
        return {"rich_text": _rich_text_content(value)}
    if property_type == PropertyType.FILES:
        # valueは{"name": str, "url": str}の辞書のリストを想定（外部URL参照方式）。
        # Notion側にファイル本体をアップロードするのではなく、既存の外部ストレージ
        # （Google Drive等）へのリンクとして登録する（2026-08-10、Zoho添付ファイル移行で導入）。
        return {
            "files": [
                {"type": "external", "name": f["name"], "external": {"url": f["url"]}}
                for f in _as_id_list(value)
            ]
        }
    raise ValueError(f"unsupported PropertyType for Notion conversion: {property_type!r}")


def build_notion_properties(properties: dict[str, Any], schema: DatabaseSchema) -> dict[str, Any]:
    """内部の`properties`辞書を、Notion APIのプロパティ形式の辞書へ一括変換する。"""
    return {
        name: build_notion_property_value(schema.get_property(name).property_type, value)
        for name, value in properties.items()
    }


# ページ作成のタイムアウト（秒）。既定の10秒ではNotion側の処理が終わる前に読み取り
# タイムアウトへ倒れる例が実際にあった（2026-08-28、external_id=62172）。
_CREATE_PAGE_TIMEOUT_SECONDS = 25.0

# 作成失敗後の回収で「直前に作られた」とみなす時間幅（分）。広げるほど、たまたま同名の
# 既存ページを誤って掴むリスクが上がる。
_CREATE_RECOVERY_WINDOW_MINUTES = 5
# 回収照会の試行回数と間隔。Notionの検索インデックスは作成直後のページを即座には返さず、
# 1回だけ引いて0件だと「作られていない」と誤判定する。2026-08-31に本番で発生
# （Zoho 22334000002657016 のページは作られていたのに0件で回収できず、IdMappingが
# 登録されないまま孤児になった）。0件のときだけ待って引き直す。
_CREATE_RECOVERY_ATTEMPTS = 3
_CREATE_RECOVERY_RETRY_DELAY_SECONDS = 2.0


class HttpNotionClient:
    """Notion API `GET/POST/PATCH /v1/pages` を用いた `NotionClient` Protocol実装。

    `db_key`（`src/db_schema/registry.py`のスキーマキー）と`database_id`（Notion側のDB ID）を
    それぞれ1つに固定してインスタンス化する。get_page()はNotionページの`properties`を
    内部形式（プロパティ名→生の値のフラットな辞書）へ変換して返す。加えて、ページの
    実際の最終更新日時（生レスポンスの`last_edited_time`）を`NOTION_LAST_EDITED_TIME_KEY`
    キーで合成して返す（コンフリクト判定でNotion側の`updated_at`として使われる）。
    """

    def __init__(
        self,
        db_key: str,
        database_id: str,
        *,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        notion_version: str = _NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._db_key = db_key
        self._database_id = database_id
        self._api_key = api_key if api_key is not None else os.environ.get("NOTION_API_KEY")
        if not self._api_key:
            raise ValueError(
                "NOTION_API_KEY environment variable (or api_key argument) is required but not set"
            )
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rate_limit_retries = max_rate_limit_retries
        self._backoff_base = backoff_base

    @property
    def _schema(self) -> DatabaseSchema:
        return get_schema(self._db_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self._notion_version,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        idempotent: bool = True,
        timeout: float | None = None,
    ) -> requests.Response:
        """`timeout`未指定時はインスタンス既定値を使う。ページ作成のように、既定
        (`DEFAULT_TIMEOUT_SECONDS`=10秒)では足りずに読み取りタイムアウトへ倒れうる
        呼び出しだけ、呼び出し側で個別に延ばす。"""
        return request_with_retry(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            json_body=json_body,
            timeout=self._timeout if timeout is None else timeout,
            max_retries=self._max_retries,
            max_rate_limit_retries=self._max_rate_limit_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/pages/{page_id}")
        if response.status_code == 404:
            return None
        raise_for_error(response, NotionApiError)
        # shirokuma-secレビューBLOCKER対応（2026-08-28）: raise_for_error()は2xxなら何もしない
        # ため、「HTTP 200だがボディの構造が想定と違う」場合（`page`が辞書でない、`properties`の
        # 各値が辞書でない等）は生の`AttributeError`/`TypeError`/`KeyError`/`IndexError`が飛び、
        # `ApiError`でも`requests.exceptions.RequestException`でもないため呼び出し元
        # （`Dispatcher`）が握っている例外の型をすり抜けてしまう。`kintone_client.py`の
        # `get_record()`と同じ書き方でここもNotionApiErrorへ正規化する。
        try:
            page = response.json()
            result: dict[str, Any] = {}
            for name, value in (page.get("properties") or {}).items():
                prop_type = value.get("type")
                if prop_type not in PARSEABLE_NOTION_PROPERTY_TYPES:
                    # formula/rollup/files/created_time等、parse_notion_property_value()が
                    # 未対応の型はページ取得全体を落とさず読み飛ばす（実運用で案件管理DBの
                    # 粗利/契約スピード等のFORMULA型プロパティに対して発生した
                    # `ValueError: unsupported Notion property type: 'formula'`の修正）。
                    # スキーマ/実データの型が乖離しているケース（=今回の本番障害の原因そのもの）を
                    # 見逃さないよう、`notion_webhook.py`の同種のケース（未知プロパティのスキップ、
                    # 「ignoring unknown Notion property」）と同様にwarningレベルで残す。
                    logger.warning(
                        "ignoring unparseable Notion property '%s' (type=%s) for db_key=%r, "
                        "page_id=%s (not in PARSEABLE_NOTION_PROPERTY_TYPES)",
                        name,
                        prop_type,
                        self._db_key,
                        page_id,
                    )
                    continue
                result[name] = parse_notion_property_value(value)
            last_edited_time = page.get("last_edited_time")
            if last_edited_time:
                result[NOTION_LAST_EDITED_TIME_KEY] = parse_iso_datetime(last_edited_time)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise NotionApiError(response.status_code, extract_error_message(response)) from exc
        return result

    def query_all_pages(
        self, *, page_size: int = 100, filter: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Notion API `POST /v1/databases/{database_id}/query` で当DBのページを取得する。

        `start_cursor`/`has_more`でページングしながら全件取得し、Notion APIの生ページ
        オブジェクト（`id`, `properties`等を含む。`get_raw_page`と同じ「生JSON」方針）の
        リストをそのまま返す。読み取り専用の冪等操作のためidempotent=True（既定）で呼ぶ。

        `filter`（省略可）にNotion Query Database APIのフィルタオブジェクト
        （例: `{"property": "メールアドレス", "email": {"equals": "..."}}`）を渡すと、
        クライアント側で全件取得してから絞り込むのではなく、Notion API側で絞り込んだ
        結果のみを取得できる（呼び出し元がDB全件をクライアント側でフィルタしている箇所を、
        件数が多いDBで軽量化する用途を想定）。省略時は従来通り当DB全件を返す。
        """
        def _post(request_body: dict[str, Any]) -> dict[str, Any]:
            response = self._request(
                "POST", f"/databases/{self._database_id}/query", json_body=request_body
            )
            raise_for_error(response, NotionApiError)
            return response.json()

        # **1万件の壁を越える**（2026-09-01）。Notionは1クエリ1万件までしか返さず、
        # しかも has_more=false を返すので「全部取れた」ように見える。
        # 案件管理のPostgresミラーも取引先名インデックスもここを通っており、
        # 静かに欠けていた（`src/sync_engine/clients/_notion_paging.py`参照）。
        return query_all_with_keyset(
            _post, base_filter=filter, page_size=page_size, label=self._db_key or "notion"
        )

    def query_page(
        self,
        *,
        page_size: int = 100,
        filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Notion API `POST /v1/databases/{database_id}/query` を1回だけ呼び、ページングしない。

        `query_all_pages`と異なり`has_more`を追わず、先頭`page_size`件で打ち切る（生ページ
        オブジェクトのリストをそのまま返す点は`query_all_pages`と同じ）。取引先マスターDB
        （実測約6.2万件）・連絡先DBのような大規模DBを都度全件取得するのは重すぎる、
        検索UI・1社スコープの関連レコード取得のような「上位N件だけ分かればよい」用途向け
        （`src/api/client_360_service.py`参照）。読み取り専用の冪等操作のためidempotent=True
        （既定）で呼ぶ。
        """
        body: dict[str, Any] = {"page_size": page_size}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        response = self._request("POST", f"/databases/{self._database_id}/query", json_body=body)
        raise_for_error(response, NotionApiError)
        data = response.json()
        return data.get("results") or []

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        """Notion API `GET /v1/pages/{page_id}` の生レスポンスJSONをそのまま返す。

        `get_page`（`properties`を内部値のフラット辞書へ変換して返す）とは異なり、
        `id`/`parent`/`last_edited_time`/`properties`を含むレスポンスをそのまま返す。
        `webhook_handlers/notion_webhook.py`の`NotionPageClient` Protocolを満たし、
        `fetch_and_normalize_notion_page`（Webhookプロキシ層）から利用される。
        """
        response = self._request("GET", f"/pages/{page_id}")
        raise_for_error(response, NotionApiError)
        page: dict[str, Any] = response.json()
        return page

    def _recover_created_page_id(self, properties: dict[str, Any]) -> str | None:
        """`create_page()`の通信が例外で終わったあと、「実は作成されていた」ページのIDを探す。

        **保守的に、確実なときだけ回収する**。タイトル（`title`型プロパティ）が完全一致し、
        かつ作成時刻が直近`_CREATE_RECOVERY_WINDOW_MINUTES`分以内のページが**ちょうど1件**の
        ときだけそのIDを返す。0件（＝そもそも作られていない）でも、2件以上（＝同名の別ページが
        あり、どれが今作ったものか判別できない）でも`None`を返し、呼び出し元は従来どおり
        例外を伝播させる。

        誤って既存の別ページを掴むと、無関係なレコード同士がIdMappingで結び付き、以後の同期で
        互いの値を上書きし合う。**回収できないこと（＝人が目視で確認する）より、間違ったページを
        掴むことの方が明確に有害**なので、少しでも曖昧なら回収しない。

        照会そのものが失敗した場合も`None`を返す（回収は最善努力であり、ここで新しい例外を
        持ち込んで元の失敗を覆い隠さない）。
        """
        title_property = self._schema.title_property
        title_value = properties.get(title_property.name)
        if not isinstance(title_value, str) or not title_value:
            return None
        created_on_or_after = (
            datetime.now(timezone.utc) - timedelta(minutes=_CREATE_RECOVERY_WINDOW_MINUTES)
        ).isoformat()
        query_filter = {
            "and": [
                {"property": title_property.name, "title": {"equals": title_value}},
                {
                    "timestamp": "created_time",
                    "created_time": {"on_or_after": created_on_or_after},
                },
            ]
        }
        for attempt in range(1, _CREATE_RECOVERY_ATTEMPTS + 1):
            try:
                pages = self.query_page(
                    page_size=2,  # 「ちょうど1件か」を判定できればよいので2件で足りる。
                    filter=query_filter,
                )
            except (NotionApiError, requests.exceptions.RequestException):
                logger.warning(
                    "create_page: 作成失敗後の回収照会にも失敗したため、回収せずに元の例外を"
                    "伝播させます（作成されたかどうかは不明のままです）"
                )
                return None
            if len(pages) == 1:
                page_id = pages[0].get("id")
                return page_id if isinstance(page_id, str) and page_id else None
            if len(pages) > 1:
                # 同名の別ページがあり、どれが今作ったものか判別できない。待っても解決しない。
                logger.warning(
                    "create_page: 作成失敗後の回収照会で該当ページが%d件だったため回収しません"
                    "（1件のときだけ回収します）",
                    len(pages),
                )
                return None
            # 0件。インデックス反映待ちの可能性があるので、間を置いて引き直す。
            if attempt < _CREATE_RECOVERY_ATTEMPTS:
                logger.info(
                    "create_page: 作成失敗後の回収照会が0件でした（%d/%d回目）。Notionの検索"
                    "インデックス反映待ちの可能性があるため%.1f秒後に引き直します",
                    attempt,
                    _CREATE_RECOVERY_ATTEMPTS,
                    _CREATE_RECOVERY_RETRY_DELAY_SECONDS,
                )
                time.sleep(_CREATE_RECOVERY_RETRY_DELAY_SECONDS)
        logger.warning(
            "create_page: 作成失敗後の回収照会が%d回とも0件だったため回収しません"
            "（ページが作られていないか、インデックス反映がさらに遅れている）",
            _CREATE_RECOVERY_ATTEMPTS,
        )
        return None

    def create_page(self, properties: dict[str, Any]) -> str:
        """ページを1件作成し、作成されたページIDを返す。

        通信が例外で終わった場合は`_recover_created_page_id()`で「実は作成されていた」ケースの
        回収を試みる（2026-08-28）。Notion側が処理を終えていてもレスポンスが返る前に読み取り
        タイムアウトすると、**ページは作られたのにこちらはIDを知らない**という状態になり、
        呼び出し元（`Dispatcher._try_create_new_record()`）はIdMappingを登録できない。その結果、
        次に同じレコードの更新Webhookが届いたときに「マッピングが無い＝未作成」と判断して
        **2枚目のページを作ってしまう**。実際に2026-08-28、external_id=62172でこの状態が発生した。
        """
        body = {
            "parent": {"database_id": self._database_id},
            "properties": build_notion_properties(properties, self._schema),
        }
        # 作成系（非冪等）操作のため、タイムアウト/5xx時の重複ページ作成を避けリトライしない。
        # タイムアウトだけは既定の10秒から延ばす（62172の実例が読み取りタイムアウトだったため。
        # リトライしない代わりに、1回の試行が完了するまで待つ側へ倒す）。
        try:
            response = self._request(
                "POST",
                "/pages",
                json_body=body,
                idempotent=False,
                timeout=_CREATE_PAGE_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            recovered_page_id = self._recover_created_page_id(properties)
            if recovered_page_id is None:
                raise
            logger.warning(
                "create_page: 通信が%sで終わりましたが、直前に作成されたとみられるページを"
                "1件だけ特定できたため、そのページIDを作成結果として返します"
                "(page_id=%r、重複ページの作成を防ぐための回収)",
                type(exc).__name__,
                recovered_page_id,
            )
            return recovered_page_id
        raise_for_error(response, NotionApiError)
        try:
            # shirokuma-secレビューWARN対応（2026-08-27）: raise_for_error()通過後（2xx）でも
            # ボディが期待した形でない場合に生のKeyErrorが飛ばないよう正規化する。詳細な理由は
            # `zoho_client.py`冒頭の同種コメント参照。
            page_id: str = response.json()["id"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise NotionApiError(response.status_code, extract_error_message(response)) from exc
        record_notion_write(
            db_key=self._db_key,
            notion_page_id=page_id,
            action="create",
            before=None,
            after=self._resolve_for_audit(properties),
        )
        return page_id

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        # 監査ログ（データ監査ログ、2026-08-17）の差分抽出用に、更新直前のページ現在値を
        # 読んでおく（`docs/audit_log_note.md`参照）。update_page呼び出しごとにGETが1回
        # 追加でNotion APIへ飛ぶことになる（書き込み系リクエスト数が実質倍になる）が、
        # Notion APIのレート制限は実測でおおむね平均3req/秒程度であり、本プロジェクトの
        # 書き込み頻度（Webhook契機の逐次処理が中心で、一括移行のような大量バルク処理は
        # `src/migration/`側で別途レート制限リトライを備えている）に対しては許容範囲と判断した。
        # 取得に失敗した場合（ページ削除・権限エラー・API障害・テストでのモック未設定等）は
        # 監査ログの記録自体を諦めるのみで、本来のPATCH処理には影響させない
        # （詳細は`_fetch_current_values_for_audit`/`src/audit_log/recorder.py`参照）。
        before = self._fetch_current_values_for_audit(page_id, properties)
        body = {"properties": build_notion_properties(properties, self._schema)}
        response = self._request("PATCH", f"/pages/{page_id}", json_body=body)
        raise_for_error(response, NotionApiError)
        record_notion_write(
            db_key=self._db_key,
            notion_page_id=page_id,
            action="update",
            before=self._resolve_for_audit(before) if before is not None else None,
            after=self._resolve_for_audit(properties),
        )

    def _resolve_for_audit(self, values: dict[str, Any]) -> dict[str, Any]:
        """監査ログに記録する直前、RELATION/USER型の値を人間が読める表示名へ解決する
        （obasan-qualityレビューWARN対応、2026-08-17。詳細は`notion_display_resolver.py`
        参照）。解決に使うのは`values`のコピーのみで、Notion APIへの実際の書き込みに使う
        `properties`自体は書き換えない。"""
        return resolve_display_values(self._db_key, values, actor_source=get_actor().source)

    def _fetch_current_values_for_audit(
        self, page_id: str, properties: dict[str, Any]
    ) -> dict[str, Any] | None:
        """監査ログの「変更前」の値として、`properties`に含まれるプロパティ名分だけ現在値を
        読む。取得できなければNoneを返す（呼び出し元の`record_notion_write`はNoneの場合、
        誤った内容を記録するより記録自体をスキップする設計）。"""
        try:
            current = self.get_page(page_id)
        except Exception:
            logger.warning(
                "failed to fetch current values for audit log before update_page "
                "(db_key=%r, page_id=%s); skipping audit log for this update",
                self._db_key,
                page_id,
                exc_info=True,
            )
            return None
        if current is None:
            return None
        return {name: current.get(name) for name in properties}

    def archive_page(self, page_id: str) -> None:
        response = self._request("PATCH", f"/pages/{page_id}", json_body={"archived": True})
        raise_for_error(response, NotionApiError)
