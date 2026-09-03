"""配信停止リンクの署名（純粋関数のみ、2026-09-03）。

■ トークンをDBに貯めない

連絡先は実測3,782件ある。全員ぶんのトークン行を先に作ると、送っていない相手の行まで
できるうえ、鍵が漏れたときに作り直す対象が3,782行になる。ここでは
`HMAC-SHA256(秘密鍵, 連絡先ページID)`をその場で計算する（ステートレス）。

```
   URL   {ダッシュボードのURL}/unsubscribe?c=<連絡先ページID>&t=<署名>
   検証  同じ鍵で計算し直して一致するか見るだけ。DBを引かない
   失効  BULK_EMAIL_UNSUBSCRIBE_SECRET を変えれば発行済みリンクが一斉に無効になる
```

**鍵を変えると、過去に送ったメールの配信停止リンクが全部使えなくなる。**
配信停止できないメールを撒いた状態は特定電子メール法に反するので、鍵はローテーション
しない前提で扱う（漏洩時のみ、停止済みの連絡先を`ContactMailPreference`から
拾える状態を確認したうえで変える）。

■ ページIDの表記ゆれを吸収する

NotionのページIDはハイフン有り(`3ced8ea8-...`)と無しの両方の形で流通する。
署名の対象を`_normalize_page_id()`に通した形（小文字・ハイフン無し）に固定しないと、
Python側が発行したリンクをTypeScript側が検証したときに、同じIDなのに一致しない。
正規化は`src/bulk_email/ids.py`の`normalize_page_id()`に一本化してある。
`dashboard/lib/bulkEmailUnsubscribe.ts`が同じ正規化と同じHMACを実装している
（片方だけ直すと配信停止リンクが全部壊れるので、必ず両方を直すこと）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import quote

from src.bulk_email.ids import normalize_page_id

_SECRET_ENV_VAR = "BULK_EMAIL_UNSUBSCRIBE_SECRET"


def load_secret() -> str:
    """署名鍵を環境変数から読む。未設定なら空文字（プレビューがBLOCKERを出す）。"""
    return (os.environ.get(_SECRET_ENV_VAR) or "").strip()


def build_token(secret: str, contact_page_id: str) -> str:
    """連絡先ページIDに対する署名を返す（URLセーフなbase64、パディング無し）。"""
    if not secret:
        raise ValueError(f"{_SECRET_ENV_VAR} is not set")
    digest = hmac.new(
        secret.encode("utf-8"),
        normalize_page_id(contact_page_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_token(secret: str, contact_page_id: str, token: str) -> bool:
    """署名が正しいか。比較は`compare_digest`（先頭何文字まで合っているかを漏らさない）。"""
    if not secret or not token:
        return False
    try:
        expected = build_token(secret, contact_page_id)
    except ValueError:
        return False
    return hmac.compare_digest(expected, token)


def build_unsubscribe_url(base_url: str, contact_page_id: str, token: str) -> str:
    """本文に載せる配信停止URLを組み立てる。"""
    base = (base_url or "").rstrip("/")
    page_id = quote(normalize_page_id(contact_page_id), safe="")
    return f"{base}/unsubscribe?c={page_id}&t={quote(token, safe='')}"
