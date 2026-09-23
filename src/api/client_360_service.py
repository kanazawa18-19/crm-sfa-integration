"""顧客360度ビュー（取引先1社の案件・連絡先・アクション履歴を1画面に集約）向けの
オーケストレーション層。

`src/api/dashboard_service.py`の`NotionDataSource`/`search_projects`と実装パターンは
揃えるが、以下の点が異なる。

- キャッシュを一切使わない。`NotionDataSource`は全社ダッシュボード向けにプロセス内
  キャッシュ（`_cached`）を持つが、本モジュールは1営業が1社を都度参照する用途であり、
  鮮度（直近の書き込みが即座に反映されること）を優先する。
- 取引先マスターDB・連絡先DBは実測約6.2万件規模のため、`query_all_pages()`（全件取得）
  ではなく`query_page()`（1回のクエリで打ち切る軽量版）を使う。検索は先頭
  `_MAX_SEARCH_RESULTS`件のみ、1社スコープの関連レコード取得も先頭`_RELATED_PAGE_SIZE`件
  までしか見ない前提（通常の営業案件・連絡先・アクション件数であれば十分収まる）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import requests

from src.api.notion_display import (
    page_to_display_dict,
    project_page_to_mirror_record,
    resolve_person_name,
)
from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.api.reply_timing_service import build_for_contact_page_ids as build_reply_timing
from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA
from src.sync_engine.clients._http import INTERACTIVE_MAX_RATE_LIMIT_RETRIES
from src.sync_engine.clients.notion_client import HttpNotionClient, NotionApiError

logger = logging.getLogger(__name__)

_MAX_SEARCH_RESULTS = 20
_RELATED_PAGE_SIZE = 100
# `search_contacts()`が1リクエストで取引先名を解決しに行く「異なるclient_id」の上限
# （ChatGPTレビューWARN対応、2026-09-24）。検索1回で最大20社ぶん`get_raw_page()`を
# 直列で叩くと、Notionのレート制限（3 req/s）に対して重すぎ、素早い再検索で429を
# 自家発生させうる。超えた分は`STATUS_取引先名_未取得`にする（`notion_page_id`は
# 常に返すため、360ビューへ飛ぶボタン自体は出せる）。
_MAX_CLIENT_NAME_LOOKUPS = 5

# 取引先マスターDB/連絡先DB/案件管理DB/アクション履歴DBのプロパティ名。
PROP_取引先名 = "取引先名"
PROP_名前 = "名前"
PROP_部署 = "部署"
PROP_役職 = "役職"
# アクション履歴DBの「取引先マスター」relationは絵文字込みの実プロパティ名
# （src/db_schema/action.py参照。過去に実データとのプロパティ名不一致で事故が
# 起きているため、必ずこの定数経由で参照すること）。
PROP_取引先マスター_ACTION = "👨‍👩‍👧‍👦 取引先マスター"
PROP_取引先マスター = "取引先マスター"
PROP_担当営業 = "担当営業"

# `search_contacts()`が添える取引先名の解決状況（obasan-quality等レビューWARN対応、
# 2026-09-24）。「2件目以降は設計上引かない」と「取得を試みたが失敗した」はどちらも
# `取引先名: None`に潰れると画面側で区別できず、両方「取得できませんでした」という
# 誤った表示になってしまうため、`取引先名_status`として別に持つ。
STATUS_取引先名_解決済み = "resolved"
STATUS_取引先名_未取得 = "not_fetched"
STATUS_取引先名_取得失敗 = "failed"


class Client360DataSource:
    """取引先マスターDB・連絡先DB・案件管理DB・アクション履歴DBのNotionページを表示用dictへ
    変換して取得するデータソース。

    `NotionDataSource`（`dashboard_service.py`）と異なり、キャッシュを一切使わない
    （モジュールdocstring参照）。各`*_client`（`query_page`/`get_raw_page`を持つ
    オブジェクト）・`user_directory`（`resolve`/`resolve_many`を持つオブジェクト）を
    注入できる（未指定時は実際のNotion APIを叩く`HttpNotionClient`/`NotionUserDirectory`を
    使う）。テストではこれらをフェイク実装に差し替える。
    """

    def __init__(
        self,
        *,
        client_master_client: Any | None = None,
        contact_client: Any | None = None,
        project_client: Any | None = None,
        action_client: Any | None = None,
        user_directory: Any | None = None,
        reply_timing_builder: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> None:
        # 連絡先ごとの返信傾向（2026-09-03）。Notionではなく自前のPostgres(EmailLog)を
        # 読むため、他のNotionクライアントと同じくここで差し替え可能にする
        # （差し替え口が無いと、テストでは例外が握り潰されて素通りしてしまう）。
        self._build_reply_timing = reply_timing_builder or build_reply_timing
        self._client_master_client = client_master_client or HttpNotionClient(
            CLIENT_MASTER_SCHEMA.key,
            CLIENT_MASTER_SCHEMA.notion_database_id,
            max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        )
        self._contact_client = contact_client or HttpNotionClient(
            CONTACT_SCHEMA.key,
            CONTACT_SCHEMA.notion_database_id,
            max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        )
        self._project_client = project_client or HttpNotionClient(
            PROJECT_SCHEMA.key,
            PROJECT_SCHEMA.notion_database_id,
            max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        )
        self._action_client = action_client or HttpNotionClient(
            ACTION_SCHEMA.key,
            ACTION_SCHEMA.notion_database_id,
            max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
        )
        if user_directory is not None:
            self._user_directory = user_directory
        else:
            from src.api.user_directory import NotionUserDirectory

            self._user_directory = NotionUserDirectory(
                max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES
            )

    def search_clients(self, query: str) -> dict[str, Any]:
        """取引先名の部分一致でNotion API側を絞り込んで検索する（`search_projects`と同様、
        選択に必要な最小限の項目のみを返す）。

        `query_page()`は1回のクエリで打ち切る設計のため、Notion側の真の一致件数は分からない
        （`query_all_pages()`のように全件取得すれば分かるが、取引先マスターDB規模では重すぎる
        ため意図的に避けている）。そのため`total_matched`という「正確な件数」を装う代わりに、
        `_MAX_SEARCH_RESULTS`件を超えて一致する可能性があるかどうかを`truncated`として返す
        （obasan-qualityレビューBLOCKER対応、2026-08-18。元実装は`total_matched = len(clients)`
        となり「他に◯件該当」表示が常に0件を返す実質デッドコードになっていた）。
        """
        normalized_query = query.strip()
        if not normalized_query:
            return {"clients": [], "truncated": False}

        pages = self._client_master_client.query_page(
            filter={"property": PROP_取引先名, "title": {"contains": normalized_query}},
            page_size=_MAX_SEARCH_RESULTS + 1,
        )
        truncated = len(pages) > _MAX_SEARCH_RESULTS
        pages = pages[:_MAX_SEARCH_RESULTS]
        clients: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        for page in pages:
            record, skipped = page_to_display_dict(page, CLIENT_MASTER_SCHEMA)
            clients.append(
                {
                    "notion_page_id": record["notion_page_id"],
                    "取引先名": record.get(PROP_取引先名) or "",
                }
            )
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "search_clients: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                CLIENT_MASTER_SCHEMA.key,
                sorted(skipped_properties),
            )
        return {"clients": clients, "truncated": truncated}

    def search_contacts(self, query: str) -> dict[str, Any]:
        """連絡先名の部分一致でNotion API側を絞り込んで検索する。`truncated`の意味は
        `search_clients`と同じ。

        連絡先から取引先360ビューへ辿れるよう、各件に`取引先マスター`relationの
        参照先を`取引先`（`[{"notion_page_id":..., "取引先名":..., "取引先名_status":...}]`）
        として添える（2026-09-24、会社名の表記ゆれで取引先名検索が0件になる問題への対応）。
        `取引先名`はNotion APIをもう1回叩かないと分からないため、N+1を野放しにしないよう
        先頭`_MAX_SEARCH_RESULTS`件・各件の先頭relation1件までに限って`_resolve_client_name`
        で引く（2件目以降のrelationは`notion_page_id`のみで`取引先名: None`・
        `取引先名_status: "not_fetched"`。`notion_page_id`は常に返すため、画面側は
        取引先名が無くても360ビューへのボタンを出せる。ChatGPTレビューWARN対応、
        2026-09-24。以前はNotionページへのリンクのみにしていたが、それでは
        「連絡先名から取引先360ビューへ飛ぶ」という本来の目的が2件目以降で崩れていた）。
        検索結果の中で同じ会社の社員が複数ヒットすると、先頭relationが同じ`client_id`に
        なることがあるため、1回のリクエスト内では`client_id`ごとに1回しか
        `get_raw_page()`を呼ばないよう`_resolved_client_names`でメモ化する
        （obasan-qualityレビューWARN対応）。加えて、1リクエストで名前解決を試みる
        「異なるclient_id」自体の数も`_MAX_CLIENT_NAME_LOOKUPS`件までに制限する
        （ChatGPTレビューWARN対応、2026-09-24。メモ化だけでは、20件の検索結果が
        20社ぶん別々のclient_idを持つ最悪ケースで`get_raw_page()`が直列20回になり、
        Notionのレート制限に対して重すぎるため）。上限を超えた分は`not_fetched`
        （ID自体は返るので360ビューへのボタンは出せる）。

        同姓同名の連絡先を画面で区別できるよう`部署`/`役職`も添える（既にNotion側の
        プロパティとして持っているため、追加のAPI呼び出しは不要。INFO対応、2026-09-24）。
        """
        normalized_query = query.strip()
        if not normalized_query:
            return {"contacts": [], "truncated": False}

        pages = self._contact_client.query_page(
            filter={"property": PROP_名前, "title": {"contains": normalized_query}},
            page_size=_MAX_SEARCH_RESULTS + 1,
        )
        truncated = len(pages) > _MAX_SEARCH_RESULTS
        pages = pages[:_MAX_SEARCH_RESULTS]
        contacts: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        # client_id -> (取引先名, status)。このリクエスト内でのみ有効な一時キャッシュ
        # （`Client360DataSource`自体は鮮度優先でキャッシュを持たない設計、モジュール
        # docstring参照。ここは同一リクエスト内の重複呼び出し防止のためだけの局所メモ化）。
        resolved_client_names: dict[str, tuple[str | None, str]] = {}
        for page in pages:
            record, skipped = page_to_display_dict(page, CONTACT_SCHEMA)
            client_ids = record.get(PROP_取引先マスター) or []
            clients: list[dict[str, Any]] = []
            for index, client_id in enumerate(client_ids):
                if index != 0:
                    clients.append(
                        {
                            "notion_page_id": client_id,
                            "取引先名": None,
                            "取引先名_status": STATUS_取引先名_未取得,
                        }
                    )
                    continue
                if client_id in resolved_client_names:
                    name, status = resolved_client_names[client_id]
                elif len(resolved_client_names) >= _MAX_CLIENT_NAME_LOOKUPS:
                    # 上限到達後は新規client_idの名前解決を試みない(get_raw_page()を
                    # 呼ばない)。resolved_client_namesへは入れない
                    # (このclient_idが後で上限緩和されて解決されるわけではないが、
                    # 同じclient_idを何度も上限判定するだけで済み、副作用は無い)。
                    name, status = None, STATUS_取引先名_未取得
                else:
                    name, status = self._resolve_client_name(client_id)
                    resolved_client_names[client_id] = (name, status)
                clients.append(
                    {"notion_page_id": client_id, "取引先名": name, "取引先名_status": status}
                )
            contacts.append(
                {
                    "notion_page_id": record["notion_page_id"],
                    "名前": record.get(PROP_名前) or "",
                    # 同姓同名を区別できるよう部署・役職も添える(INFO対応、2026-09-24)。
                    "部署": record.get(PROP_部署) or None,
                    "役職": record.get(PROP_役職) or None,
                    "取引先": clients,
                }
            )
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "search_contacts: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                CONTACT_SCHEMA.key,
                sorted(skipped_properties),
            )
        return {"contacts": contacts, "truncated": truncated}

    def _resolve_client_name(self, client_id: str) -> tuple[str | None, str]:
        """`search_contacts`が添える取引先名を1件だけAPIで引く（先頭relationのみ）。

        `self._client_master_client`は`get_client_360`でも使っている`HttpNotionClient`
        （または同等のフェイク）で、単一ページ取得メソッド`get_raw_page`を必ず持つため、
        メソッドの有無を防御的にチェックしない。

        取得失敗は検索結果全体を落とさずステータス`"failed"`へフォールバックする。
        `NotionApiError`（404・権限エラー等、`raise_for_error`が正規化する）だけでなく
        `requests.exceptions.RequestException`（タイムアウト・接続断等、HTTPリクエスト自体が
        失敗した場合はNotion側のエラーレスポンスに正規化されない）も同様に握る。20件の
        検索結果のうち1件がタイムアウトしただけで検索全体が落ちるのを避けるため
        （obasan-qualityレビューWARN対応、2026-09-24）。
        """
        try:
            raw_page = self._client_master_client.get_raw_page(client_id)
        except (NotionApiError, requests.exceptions.RequestException) as exc:
            logger.warning(
                "search_contacts: 取引先名の解決に失敗しました client_id=%s exc_type=%s",
                client_id,
                type(exc).__name__,
            )
            return None, STATUS_取引先名_取得失敗
        record, _ = page_to_display_dict(raw_page, CLIENT_MASTER_SCHEMA)
        return record.get(PROP_取引先名) or None, STATUS_取引先名_解決済み

    def get_client_360(self, client_id: str) -> dict[str, Any] | None:
        """取引先1社について、取引先概要・配下の案件・連絡先・アクション履歴をまとめて返す。

        取引先が存在しない（404）場合はNoneを返す。
        """
        try:
            raw_page = self._client_master_client.get_raw_page(client_id)
        except NotionApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        client, _ = page_to_display_dict(raw_page, CLIENT_MASTER_SCHEMA)

        projects = self._fetch_projects(client_id)
        contacts = self._fetch_contacts(client_id)
        actions = self._fetch_actions(client_id)

        # 連絡先ごとの返信傾向（返信ラグ・返ってきやすい時間帯、2026-09-03）。
        # `contacts`の各要素に混ぜず別キーで返す — `contacts`はNotionのプロパティを
        # そのまま写したものであり、Notionに無い算出値を紛れ込ませると、画面側から
        # 「どれがNotionの値でどれが計算結果か」が見分けられなくなるため。
        reply_timing = self._build_reply_timing(
            [c["notion_page_id"] for c in contacts if c.get("notion_page_id")]
        )

        return {
            "client": client,
            "projects": projects,
            "contacts": contacts,
            "actions": actions,
            "reply_timing": reply_timing,
        }

    def fetch_client_contacts(self, client_id: str) -> dict[str, Any] | None:
        """取引先1社の「取引先名」と連絡先だけを取る（一斉配信の宛先組み立て用、2026-09-03）。

        `get_client_360()`と違い、案件・アクション履歴・返信傾向は取らない。宛先を作るのに
        要らないものまでNotionに取りに行くと、取引先を10社選んだだけでAPI呼び出しが
        4倍になるため。

        戻り値の`truncated`は「連絡先が`_RELATED_PAGE_SIZE`件で打ち切られたかもしれない」の意味。
        **これを黙って捨てないこと。** 一斉配信では、打ち切りは「送ったつもりで送っていない
        相手がいる」という形で表に出る（画面まで警告を上げる）。

        取引先が存在しない（404）場合はNone。
        """
        try:
            raw_page = self._client_master_client.get_raw_page(client_id)
        except NotionApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        client, _ = page_to_display_dict(raw_page, CLIENT_MASTER_SCHEMA)

        contacts = self._fetch_contacts(client_id)
        return {
            "client_name": client.get(PROP_取引先名) or "",
            "contacts": contacts,
            "truncated": len(contacts) >= _RELATED_PAGE_SIZE,
        }

    def _fetch_projects(self, client_id: str) -> list[dict[str, Any]]:
        pages = self._project_client.query_page(
            filter={"property": PROP_取引先マスター, "relation": {"contains": client_id}},
            page_size=_RELATED_PAGE_SIZE,
        )
        records: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        for page in pages:
            record, skipped = project_page_to_mirror_record(page, self._user_directory)
            records.append(record)
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "get_client_360: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                PROJECT_SCHEMA.key,
                sorted(skipped_properties),
            )
        return records

    def _fetch_contacts(self, client_id: str) -> list[dict[str, Any]]:
        pages = self._contact_client.query_page(
            filter={"property": PROP_取引先マスター, "relation": {"contains": client_id}},
            page_size=_RELATED_PAGE_SIZE,
        )
        records: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        for page in pages:
            record, skipped = page_to_display_dict(page, CONTACT_SCHEMA)
            records.append(record)
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "get_client_360: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                CONTACT_SCHEMA.key,
                sorted(skipped_properties),
            )
        return records

    def _fetch_actions(self, client_id: str) -> list[dict[str, Any]]:
        pages = self._action_client.query_page(
            filter={
                "property": PROP_取引先マスター_ACTION,
                "relation": {"contains": client_id},
            },
            page_size=_RELATED_PAGE_SIZE,
        )
        records: list[dict[str, Any]] = []
        skipped_properties: set[str] = set()
        for page in pages:
            record, skipped = page_to_display_dict(page, ACTION_SCHEMA)
            records.append(record)
            skipped_properties |= skipped
        if skipped_properties:
            logger.warning(
                "get_client_360: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
                ACTION_SCHEMA.key,
                sorted(skipped_properties),
            )
        for record in records:
            record[PROP_担当営業] = self._resolve_assignee(record.get(PROP_担当営業))
        return records

    def _resolve_assignee(self, value: Any) -> str | None:
        """`担当営業`はrollupのため、実データでは`[[{"id":..., "name":...}]]`（rollup配列の中に
        peopleリストがネストされた形）で入ってくる（`NotionDataSource._resolve_assignee`と
        同じ防御的実装）。"""
        first = (value[0] if value else None) if isinstance(value, list) else value
        if isinstance(first, list):
            first = first[0] if first else None
        if isinstance(first, dict):
            return resolve_person_name(first, self._user_directory)
        if not first:
            return None
        return self._user_directory.resolve(str(first))


def search_clients(query: str, *, data_source: Client360DataSource | None = None) -> dict[str, Any]:
    source = data_source or Client360DataSource()
    return source.search_clients(query)


def search_contacts(query: str, *, data_source: Client360DataSource | None = None) -> dict[str, Any]:
    source = data_source or Client360DataSource()
    return source.search_contacts(query)


def get_client_360(
    client_id: str, *, data_source: Client360DataSource | None = None
) -> dict[str, Any] | None:
    source = data_source or Client360DataSource()
    return source.get_client_360(client_id)


def fetch_client_contacts(
    client_id: str, *, data_source: Client360DataSource | None = None
) -> dict[str, Any] | None:
    source = data_source or Client360DataSource()
    return source.fetch_client_contacts(client_id)
