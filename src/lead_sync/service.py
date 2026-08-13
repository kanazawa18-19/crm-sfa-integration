"""Notion連絡先DBのレコード変更を、web-engagement-tool側のLead連携API（`POST /api/leads/sync`、
メールアドレスによるupsert）へ同期するサービス層。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.lead_sync.web_engagement_tool_client import WebEngagementToolLeadSyncClient
from src.sync_engine.webhook_handlers.notion_webhook import (
    NotionPageClient,
    parse_notion_property_value,
)

logger = logging.getLogger(__name__)


def sync_contact_to_lead(
    properties: Mapping[str, Any],
    notion_page_id: str,
    *,
    notion_client: NotionPageClient,
    lead_sync_client: WebEngagementToolLeadSyncClient | None = None,
) -> dict[str, Any] | None:
    """`properties`（`SyncEvent.properties`相当の辞書、連絡先DBのプロパティ）を
    web-engagement-tool側のLeadシステムへ同期する。

    マッピング方針（`src/db_schema/contact.py`のCONTACT_SCHEMAと
    `POST /api/leads/sync`のAPI契約を突き合わせて決定したもの）:

    - `email`: `"メールアドレス"`から取得する。無い/空の場合は同期対象になり得ない
      （受信側APIではemailがupsertキーの必須項目のため）ので、同期をスキップし`None`を返す
      （`sync_next_action_date_to_calendar`が「同期すべき対象が無い」場合に`None`を返す
      のと同じ設計）。
    - `last_name`: `"名前"`（Notion側のtitleプロパティ、姓名を分けず1つの文字列として保持）の
      値をそのまま渡す。日本語の氏名は姓名の区切りが安定して取れないため、分割は行わない
      （`first_name`は送らない）。
    - `company`: `"取引先マスター"`（relation）の1件目が指す取引先マスターDBページの
      `"取引先名"`を取得する。`parse_notion_property_value`はrelationプロパティを
      関連ページIDのリストにしか解決できない（表示名は含まれない）ため、
      `notion_client.get_raw_page()`で関連ページを追加取得する。relationが空、取得失敗、
      関連ページに`"取引先名"`が無い、のいずれの場合も`company`を省略する（受信側APIでは
      任意項目であり、一時的な取得失敗で連絡先同期全体を止めるべきではないため）。
    - `phone`: `"携帯番号"`を優先し、無ければ`"直通TEL"`にフォールバックする。両方無ければ
      省略する。
    - `assigned_rep_email`: 連絡先DBには信頼できる担当者メールアドレスの項目が無いため、
      常に省略する（`案件管理`/`チェーン`のrelationをたどれば推測できなくはないが、
      別途の設計判断が必要なスコープ外の話であり、本関数では扱わない）。

    `lead_sync_client`省略時は`WebEngagementToolLeadSyncClient()`をデフォルト生成する
    （テストでは注入して差し替える）。

    この関数自体は例外を握りつぶさない（呼び出し元＝webhookハンドラ側で「失敗してもWebhook
    全体は失敗させない」という判断を行う設計とする。`sync_next_action_date_to_calendar`と
    同じ責務分担）。
    """
    email = properties.get("メールアドレス")
    if not email:
        # obasan-qualityレビューWARN対応（2026-08-13）: sync_next_action_date_to_calendarの
        # 「同期すべき対象が無い」場合の暗黙スキップと同じ設計だが、可観測性の観点では
        # 「特定の連絡先が一度も同期されない」ことを後から追えるよう、debugログだけは
        # 残す（メールアドレス未設定は連絡先として正常にありうる状態であり、warning等の
        # 障害を示すログレベルにはしない）。
        logger.debug(
            "skipping lead sync: メールアドレス未設定のためupsertキーが無い page_id=%s",
            notion_page_id,
        )
        return None

    if lead_sync_client is None:
        lead_sync_client = WebEngagementToolLeadSyncClient()

    last_name = properties.get("名前") or None
    company = _resolve_company_name(properties.get("取引先マスター"), notion_client)
    phone = properties.get("携帯番号") or properties.get("直通TEL") or None

    kwargs: dict[str, Any] = {"email": email}
    if company is not None:
        kwargs["company"] = company
    if last_name is not None:
        kwargs["last_name"] = last_name
    if phone is not None:
        kwargs["phone"] = phone

    return lead_sync_client.upsert_lead(**kwargs)


def _resolve_company_name(
    client_master_relation: Any, notion_client: NotionPageClient
) -> str | None:
    """`"取引先マスター"`relationの1件目が指す取引先マスターDBページの`"取引先名"`を取得する。

    relationが空、取得失敗、関連ページに`"取引先名"`が無い場合は`None`を返す（company省略。
    詳細は`sync_contact_to_lead`のdocstring参照）。
    """
    if not client_master_relation:
        return None

    related_page_id = client_master_relation[0]
    try:
        related_page = notion_client.get_raw_page(related_page_id)
    except Exception as exc:
        # 会社名解決はあくまで付加情報（受信側APIでも任意項目）であり、Notion APIの
        # 一時的な取得失敗（ApiError等）で連絡先同期全体（emailのupsert）を止めるべきでは
        # ないため、ここでは種類を問わず広く握りつぶしcompanyを省略する。
        #
        # obasan-qualityレビューWARN対応（2026-08-13）: ただし何もログを出さないと、
        # 認証情報失効・権限エラー等の持続的な実障害（毎回失敗する）と、単に関連ページが
        # 無い正常系とが区別できなくなる（calendar_syncの配線バグが「サイレントに動いて
        # いなかった」問題と同種のリスク）。例外の型名とrelated_page_idのみを記録し、
        # 例外メッセージ本文（Notion APIレスポンス由来でPIIを含みうる）は記録しない。
        logger.warning(
            "会社名解決に失敗しました（companyを省略して連絡先同期は続行します）: "
            "related_page_id=%s exc_type=%s",
            related_page_id,
            type(exc).__name__,
        )
        return None

    title_prop = (related_page.get("properties") or {}).get("取引先名")
    if title_prop is None:
        return None
    return parse_notion_property_value(title_prop) or None
