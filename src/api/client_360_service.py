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

# 取引先マスターDB/連絡先DB/案件管理DB/アクション履歴DBのプロパティ名。
PROP_取引先名 = "取引先名"
PROP_名前 = "名前"
# アクション履歴DBの「取引先マスター」relationは絵文字込みの実プロパティ名
# （src/db_schema/action.py参照。過去に実データとのプロパティ名不一致で事故が
# 起きているため、必ずこの定数経由で参照すること）。
PROP_取引先マスター_ACTION = "👨‍👩‍👧‍👦 取引先マスター"
PROP_取引先マスター = "取引先マスター"
PROP_担当営業 = "担当営業"


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
        `search_clients`と同じ。"""
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
        for page in pages:
            record, skipped = page_to_display_dict(page, CONTACT_SCHEMA)
            contacts.append(
                {
                    "notion_page_id": record["notion_page_id"],
                    "名前": record.get(PROP_名前) or "",
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
