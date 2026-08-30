"""外部連携の疎通診断エンドポイント（2026-08-31追加）。

各連携は認証情報が未設定でも例外を出さず「警告ログ1行で無効化」する作りのため、
同期が丸ごと止まっていても画面上は何も起きない。Vercel Hobbyのログ保持は約1時間しかなく
後追いでも追えないため、**こちらから叩いて到達を確かめる**入口を用意する。

読み取り専用。レコードの作成・更新・削除は行わない
（詳細は`src.diagnostics.integrations`のモジュールdocstring）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from src.api.auth import verify_dashboard_api_token
from src.diagnostics.integrations import run_integration_diagnostics

router = APIRouter()


@router.get(
    "/api/diagnostics/integrations",
    dependencies=[Depends(verify_dashboard_api_token)],
)
def get_integration_diagnostics(
    only: str = Query(
        default="",
        description="カンマ区切りで診断対象を絞る（例: spreadsheet,zoho）。空なら全件。",
    ),
) -> dict[str, Any]:
    """全ての外部連携に対して到達確認を行い、結果を返す。

    レスポンスの`failed`は**対処が要るものだけ**が入る。環境変数が未設定で意図的に
    無効化されている連携は`not_configured`に分けている（未設定を異常として通知すると
    誤報になり、誤報を鳴らし続けると本物の通知も無視されるため）。
    """
    targets = tuple(name.strip() for name in only.split(",") if name.strip())
    return run_integration_diagnostics(only=targets)
