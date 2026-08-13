"""Notion連絡先DBのレコードをweb-engagement-tool側のLeadシステム
（`POST /api/leads/sync`、メールアドレスによるupsert）へ反映するための単方向フック。

既存の`src/sync_engine`（Any-to-Any双方向同期）・`src/calendar_sync`（次回アクション日→
Google Calendar同期）とは完全に独立している（本パッケージはNotion→Leadシステムの一方向の
副作用処理であり、双方向同期の対象ではない）。
"""

from __future__ import annotations
