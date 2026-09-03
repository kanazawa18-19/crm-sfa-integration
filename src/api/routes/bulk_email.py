"""一斉配信のエンドポイント（2026-09-03）。

**今あるのはプレビューだけで、送信のエンドポイントは無い。**
送信経路（Gmail APIに`gmail.send`を足すか）が未決定のため意図的に用意していない。
`docs/bulk_email_design_note.md`の「出す順番（段階リリース）」を参照。

読み取りしかしないがPOSTなのは、本文テンプレート（改行を含む長文）をクエリ文字列に
載せたくないため。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth import verify_dashboard_api_token
from src.api.bulk_email_service import build_bulk_email_preview
from src.sync_engine.clients.notion_client import NotionApiError

logger = logging.getLogger(__name__)

router = APIRouter()


class BulkEmailPreviewRequest(BaseModel):
    subject: str = Field(default="", max_length=500)
    # 本文の上限。営業メールとしては十分に長く、かつ誤って巨大なデータを投げ込まれても
    # Notion問い合わせの前で弾ける程度の値。
    body: str = Field(default="", max_length=20000)
    # 差出人（この配信を作った営業担当）の表示名。`{{担当者名}}`の差し込みに使う。
    sender_name: str = Field(default="", max_length=100)
    client_page_ids: list[str] = Field(default_factory=list)


@router.post("/api/bulk-email/preview", dependencies=[Depends(verify_dashboard_api_token)])
def preview_bulk_email(request: BulkEmailPreviewRequest) -> dict:
    try:
        return build_bulk_email_preview(
            subject=request.subject,
            body=request.body,
            client_page_ids=request.client_page_ids,
            sender_name=request.sender_name,
        )
    except ValueError as exc:
        # 取引先の選びすぎ等、利用者が直せる入力の問題。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotionApiError as exc:
        logger.exception("bulk_email preview: Notionの取得に失敗しました")
        raise HTTPException(
            status_code=502, detail=f"Notionからの取得に失敗しました: {exc}"
        ) from exc
