"""Vercel Python Functionsのエントリーポイント。

Vercelは`api/`ディレクトリ配下のファイルからASGIアプリ（`app`という名前の変数）を
自動検出する（https://vercel.com/docs/functions/runtimes/python）。既存の
`src/api/app.py`のFastAPIアプリケーションをそのままre-exportするだけの薄いラッパー。
アプリケーション本体のロジックは一切ここに書かない（ローカル実行用のuvicorn起動
コマンドとエントリーポイントを共通化するため）。
"""

from __future__ import annotations

import os
import sys

# Vercelのビルド環境ではプロジェクトルートが必ずしもPYTHONPATHに含まれないため、
# `from src.api.app import app`（srcパッケージからの絶対import）が解決できるよう
# 明示的にリポジトリルートをsys.pathへ追加する。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.api.app import app  # noqa: E402
