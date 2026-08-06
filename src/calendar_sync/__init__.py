"""案件管理DB「次回アクション日」変更をweb-engagement-tool側のGoogle Calendar連携API
（`POST /api/calendar/events`）へ反映するための単方向フック。

既存の`src/sync_engine`（Any-to-Any双方向同期）とは完全に独立している
（本パッケージはNotion→カレンダーの一方向の副作用処理であり、双方向同期の対象ではない）。
"""

from __future__ import annotations
