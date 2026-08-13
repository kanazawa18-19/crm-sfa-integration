"""カレンダーイベントの参加者メールアドレスから、対象案件（Notion案件管理DB）を推定する。

マッチング方針（Rule 2、2026-08-13、金沢さん確認済み）:
1. 参加者メールアドレスのうち自社ドメイン（`INTERNAL_EMAIL_DOMAINS`）以外を対象とする
2. 各メールアドレスでNotion連絡先DBを検索し、ヒットした連絡先の`取引先マスター`
   リレーション先（複数ありうる）を集める
3. 集めた取引先マスターに紐づく案件管理DBのレコードのうち、`営業ステータス`が
   `classify_status()`で「進行中」に分類されるものだけを候補とする
4. 全参加者・全取引先を通じて候補案件の集合を作り、正確に1件に絞れた場合のみ返す。
   0件（見つからない）・複数件（確定できない）はNone（自動作成をスキップする）
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from src.db_schema.project import classify_status
from src.sync_engine.clients.notion_lookup import find_page_id_by_email

logger = logging.getLogger(__name__)

_CONTACT_EMAIL_PROPERTY = "メールアドレス"
_CONTACT_CLIENT_MASTER_PROPERTY = "取引先マスター"
_PROJECT_CLIENT_MASTER_PROPERTY = "取引先マスター"
_PROJECT_STATUS_PROPERTY = "営業ステータス"


class MatcherNotionClient(Protocol):
    """本モジュールがNotionクライアントに要求する最小インターフェース。"""

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def get_raw_page(self, page_id: str) -> dict[str, Any]: ...


def _is_internal_email(email: str, internal_domains: frozenset[str]) -> bool:
    domain = email.strip().lower().rsplit("@", 1)[-1]
    return domain in internal_domains


def _extract_relation_ids(page: dict[str, Any], property_name: str) -> list[str]:
    props = page.get("properties") or {}
    prop = props.get(property_name)
    if prop is None or prop.get("type") != "relation":
        return []
    return [item["id"] for item in prop.get("relation") or []]


def _extract_status(page: dict[str, Any], property_name: str) -> str | None:
    props = page.get("properties") or {}
    prop = props.get(property_name)
    if prop is None:
        return None
    status = prop.get("status")
    return status.get("name") if status else None


def _find_active_project_ids_for_client_master(
    project_client: MatcherNotionClient, client_master_id: str
) -> set[str]:
    """`client_master_id`（取引先マスターDBのページID）に紐づく「進行中」案件のID集合を返す。

    `classify_status()`は未知のステータス値でValueErrorを送出する設計（サイレント
    フォールバックしない方針、`src/db_schema/project.py`のdocstring参照）のため、ここでは
    1件の未知ステータスで自動化フロー全体を落とさないよう捕捉し、当該案件を「進行中扱い
    しない」側（候補から除外）に倒す。
    """
    candidates = project_client.query_all_pages(
        filter={
            "property": _PROJECT_CLIENT_MASTER_PROPERTY,
            "relation": {"contains": client_master_id},
        }
    )
    active_ids: set[str] = set()
    for page in candidates:
        status = _extract_status(page, _PROJECT_STATUS_PROPERTY)
        if status is None:
            continue
        try:
            classified = classify_status(status)
        except ValueError:
            logger.warning(
                "meeting_sync: unknown 営業ステータス value %r for project page_id=%s, "
                "excluding from candidates",
                status,
                page.get("id"),
            )
            continue
        if classified == "進行中":
            active_ids.add(page["id"])
    return active_ids


def find_matching_project(
    attendee_emails: list[str],
    contact_client: MatcherNotionClient,
    project_client: MatcherNotionClient,
    *,
    internal_domains: frozenset[str] = frozenset(),
    event_id: str | None = None,
) -> str | None:
    """`attendee_emails`から対象案件のNotion page idを1件返す。確定できなければNone。

    `event_id`（省略可）はログ出力にのみ使う。Gemini他モデルレビュー指摘（2026-08-14）:
    web-engagement-tool側のGoogleカレンダーイベントidが無いと、2リポジトリを跨いだ
    「なぜこの予定が自動検知されなかったか」の問い合わせ調査がしづらいため、判定理由の
    ログにイベントidを含められるようにする。
    """
    candidate_project_ids: set[str] = set()
    # 同じ取引先マスターに複数の参加者（連絡先）が紐づく場合、案件検索のNotion API呼び出しを
    # 取引先単位で重複させないためのdedupセット（例: 同じ会社から2名が商談に出席した場合）。
    client_master_ids_seen: set[str] = set()

    for email in attendee_emails:
        if not email or _is_internal_email(email, internal_domains):
            continue

        contact_page_id = find_page_id_by_email(contact_client, _CONTACT_EMAIL_PROPERTY, email)
        if contact_page_id is None:
            continue

        contact_page = contact_client.get_raw_page(contact_page_id)
        client_master_ids = _extract_relation_ids(contact_page, _CONTACT_CLIENT_MASTER_PROPERTY)

        for client_master_id in client_master_ids:
            if client_master_id in client_master_ids_seen:
                continue
            client_master_ids_seen.add(client_master_id)
            candidate_project_ids |= _find_active_project_ids_for_client_master(
                project_client, client_master_id
            )

    # obasan-qualityレビューWARN対応（2026-08-13）: 0件・複数件どちらでスキップされたかが
    # ログに残らないと、「なぜこの商談は自動検知されなかったのか」を後から調査する手がかりが
    # 無くなる（連絡先DB未登録なのか、取引先の紐付けが曖昧だったのかを区別できない）。
    if len(candidate_project_ids) == 1:
        return next(iter(candidate_project_ids))
    if len(candidate_project_ids) == 0:
        logger.info(
            "meeting_sync: no matching project found for attendees=%s event_id=%s",
            attendee_emails,
            event_id,
        )
    else:
        logger.info(
            "meeting_sync: %d candidate projects for attendees=%s event_id=%s, "
            "skipping (ambiguous): %s",
            len(candidate_project_ids),
            attendee_emails,
            event_id,
            candidate_project_ids,
        )
    return None
