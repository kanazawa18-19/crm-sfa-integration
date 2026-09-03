"""特定電子メール法まわりの表示（2026-09-03）。

営業目的のメールをまとめて送る以上、本文に載せないといけないものがある。
**「あとで足す」にしない。** 後付けにすると、試しに送った数十通だけが違法な形になる。

```
   ① 配信停止の方法を本文に明示し、実際に停止できること
   ② 送信者（会社名・住所・連絡先）を本文に表示すること
   ③ 停止の申し出を受けたら以後送らないこと
```

このモジュールは①②を本文の末尾に必ず付ける。③は`audience.select_recipients()`が
`ContactMailPreference`を見て除外する。

■ 会社情報が1つでも空なら送らせない

`config/bulk_email_sender.json`が初期状態では全部空になっている。
`missing_sender_fields()`が空欄を列挙し、プレビューがBLOCKERとして出す。
**推測で埋めない。** 誤った住所を数千通に載せる方が、送れないことより悪い。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "bulk_email_sender.json"

# 本文の末尾に付けた法定表示の始まりを示す目印。既に付いている本文へ二重に
# 付けないための判定にも使う（プレビューを2回通しても footer が2つにならない）。
FOOTER_MARKER = "───── 送信者・配信停止 ─────"

# 画面に出す日本語のラベル（空欄の指摘に使う）。dictの順がそのまま表示順。
_FIELD_LABELS: dict[str, str] = {
    "company_name": "会社名",
    "postal_code": "郵便番号",
    "address": "住所",
    "contact_email": "問い合わせ先メールアドレス",
    "contact_url": "問い合わせ先URL",
}


@dataclass(frozen=True)
class SenderIdentity:
    company_name: str = ""
    postal_code: str = ""
    address: str = ""
    contact_email: str = ""
    contact_url: str = ""


def load_sender_identity(config_path: Path | None = None) -> SenderIdentity:
    """`config/bulk_email_sender.json`を読み、環境変数があれば上書きする。

    ファイルが無い/壊れている場合は全項目が空の`SenderIdentity`を返す
    （呼び出し元は`missing_sender_fields()`で空欄を検出してBLOCKERにする。
    ここで例外を投げると、設定漏れが「画面が真っ白」として出てしまい原因が分からない）。
    """
    path = config_path or _CONFIG_PATH
    raw: dict[str, object] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}

    def value(field: str) -> str:
        env = os.environ.get(f"BULK_EMAIL_SENDER_{field.upper()}")
        if env is not None and env.strip():
            return env.strip()
        found = raw.get(field)
        return str(found).strip() if isinstance(found, str) else ""

    return SenderIdentity(**{field: value(field) for field in _FIELD_LABELS})


def missing_sender_fields(identity: SenderIdentity) -> list[str]:
    """空欄になっている項目の日本語ラベルを返す。"""
    return [
        label for field, label in _FIELD_LABELS.items() if not getattr(identity, field, "").strip()
    ]


def build_footer(identity: SenderIdentity, unsubscribe_url: str) -> str:
    """本文の末尾に付ける法定表示。

    `unsubscribe_url`が空のとき（署名鍵が未設定のプレビュー）は、リンクの代わりに
    その旨を1行入れる。**空のURLを載せて「一応それらしい本文」を見せない** —
    プレビューが通ったように見えて、実際には停止できないメールになるため。
    """
    lines = [
        "",
        FOOTER_MARKER,
        "配信停止をご希望の場合は、次のURLからお手続きいただけます。",
    ]
    if unsubscribe_url:
        lines.append(unsubscribe_url)
    else:
        lines.append("（配信停止URLは未生成です。署名鍵を設定するまでこのメールは送れません）")
    lines += [
        "",
        identity.company_name,
        f"〒{identity.postal_code} {identity.address}".strip(),
        f"お問い合わせ: {identity.contact_email}",
        identity.contact_url,
        "───────────────────────────",
    ]
    return "\n".join(line for line in lines if line is not None)


# 配信停止URLの見分け方（`unsubscribe.build_unsubscribe_url()`が作る形）。
# ドメインは環境によって変わるのでパス以降だけを見る。
_UNSUBSCRIBE_LINK_HINT = "/unsubscribe?c="


def contains_footer(body: str) -> bool:
    """テンプレート本文に、法定表示か**他人の配信停止リンク**が貼り付けられているか。

    **見つかったらプレビューはBLOCKERにする。** 過去に送ったメールをそのまま
    テンプレートへ貼り付けると、そこに埋まっている配信停止URLは「その時の別の宛先」
    のものになる。付いているから足さない、という扱いにすると、全員に他人のリンクが
    載ったメールが出ていく。

    **目印の行だけを見ない。** 目印（`───── 送信者・配信停止 ─────`）は
    見た目が飾りなので、貼り付けた人が「不要な区切り線」と思って消すことがある。
    その1行を消しただけで、直上に残った他人の配信停止URLは検出をすり抜ける。
    受信者がそのリンクを押すと、**まったく無関係の人の配信が停止される**
    （Geminiレビュー指摘、2026-09-03）。目印とURLの両方を見る。
    """
    text = body or ""
    return FOOTER_MARKER in text or _UNSUBSCRIBE_LINK_HINT in text


def append_footer(body: str, footer: str) -> str:
    """本文の末尾に法定表示を付ける。

    常に付ける。既に入っているかどうかの判断は`contains_footer()`で呼び出し元が行い、
    入っていた場合は差し込みではなくエラーにする（この関数は判断しない）。
    """
    return f"{(body or '').rstrip()}\n{footer}"
