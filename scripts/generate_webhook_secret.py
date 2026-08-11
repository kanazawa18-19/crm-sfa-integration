#!/usr/bin/env python3
"""`*_WEBHOOK_SECRET`（例: ZOHO_WEBHOOK_SECRET）用のランダムな共有シークレットを生成する。

`src/sync_engine/webhook_handlers/_common.py`の`verify_webhook_secret()`が比較する
X-Webhook-Secretヘッダー値として使う、暗号論的に安全なランダム文字列を標準出力へ
表示するだけの小さなユーティリティ。既存の`config/.env.example`にはこの生成手順の
precedentが無かったため、他の`*_API_TOKEN`/`*_WEBHOOK_SECRET`と同様のURL-safeな
ランダムトークンとして`secrets.token_urlsafe`を用いる。

このスクリプト自身は`config/.env`への書き込みや外部サービス呼び出しは一切行わない。
出力された値を手動で`config/.env`（ローカル）・Vercelの環境変数（本番）へ設定すること。

使い方:
    python scripts/generate_webhook_secret.py
    python scripts/generate_webhook_secret.py --bytes 48
"""

from __future__ import annotations

import argparse
import secrets

_DEFAULT_BYTES = 32


def generate_secret(num_bytes: int = _DEFAULT_BYTES) -> str:
    return secrets.token_urlsafe(num_bytes)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bytes",
        dest="num_bytes",
        type=int,
        default=_DEFAULT_BYTES,
        help=f"生成する乱数のバイト数（既定: {_DEFAULT_BYTES}）。secrets.token_urlsafeへそのまま渡す。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    print(generate_secret(args.num_bytes))


if __name__ == "__main__":
    main()
