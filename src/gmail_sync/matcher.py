"""メールアドレス1件からNotion連絡先DBのページを1件に絞り込む(2026-08-16)。

meeting_syncの案件マッチング(複数参加者から案件を絞り込む、候補0件/複数件は
スキップ)とは異なり、こちらは「送信者または受信者のメールアドレス1件→連絡先1件」
という単純な1:1ルックアップのため、既存のfind_page_id_by_email()をそのまま使うだけで
十分。専用モジュールに切り出しているのは、Notion連絡先DBのプロパティ名
(`メールアドレス`)をこのモジュール内に閉じ込め、呼び出し元(sync.py)がNotionの
プロパティ名を直接知らなくて済むようにするため。
"""

from __future__ import annotations

from src.sync_engine.clients.notion_lookup import NotionQueryClient, find_page_id_by_email

_CONTACT_EMAIL_PROPERTY = "メールアドレス"


def find_contact_page_id(contact_client: NotionQueryClient, email: str) -> str | None:
    """`email`に一致する連絡先ページIDを返す(見つからなければNone)。"""
    return find_page_id_by_email(contact_client, _CONTACT_EMAIL_PROPERTY, email)
