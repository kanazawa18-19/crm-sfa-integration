"""回収した鍵で既存の暗号文を復号できるか確かめる（読み取り専用）。

鍵は標準入力から受け取る。引数にも環境変数にも出さない（ps から見えないようにするため）。
出力は OK / NG と件数だけで、鍵も平文も表示しない。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def main() -> int:
    key = sys.stdin.read().strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        print("NG 鍵の形式が不正（64桁hexではない）")
        return 1

    dsn = ""
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", "dashboard", ".env.local")
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("DATABASE_URL_UNPOOLED="):
                dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not dsn:
        print("NG DATABASE_URL_UNPOOLED が見つからない")
        return 1

    import psycopg

    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
        rows = conn.execute(
            'SELECT "refreshTokenEnc" FROM "RepGmailConnection" '
            'WHERE "refreshTokenEnc" <> \'\' ORDER BY "connectedAt" DESC LIMIT 5'
        ).fetchall()

    if not rows:
        print("NG 検証対象の暗号文が0件")
        return 1

    os.environ["TOKEN_ENCRYPTION_KEY"] = key
    from src.gmail_sync.token_crypto import decrypt_token

    ok = 0
    for (enc,) in rows:
        try:
            if decrypt_token(enc):
                ok += 1
        except Exception:
            pass

    print(f"{'OK' if ok else 'NG'} 復号できた件数 {ok}/{len(rows)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
