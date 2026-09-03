"""本文テンプレートの差し込み（純粋関数のみ、2026-09-03）。

`{{会社名}}`のような差し込み名を、連絡先ごとの値に置き換える。

■ 置き換えは1回だけ通す（再展開しない）

`re.sub`に関数を渡して1パスで置換する。素朴に`str.replace`を差し込み名の数だけ
繰り返すと、**差し込んだ値の中に`{{…}}`が入っていた場合にそれがもう一度展開される**。
取引先名や部署名は営業が自由に入力できるNotionのテキストなので、悪意が無くても
`{{`が紛れ込みうるし、紛れ込ませれば他人の差し込み値を覗ける形になる。

■ 値が無い差し込みは「空欄で送る」ではなく呼び出し元に返す

`〇〇様`のつもりが`　様`になったメールを数百通出すのは、送らないより悪い。
`render()`は空欄になった差し込み名を`missing`で返し、`preview.py`が
その宛先を除外する（`audience.SKIP_MISSING_MERGE_VALUE`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

# 使える差し込み名と、画面のヘルプにそのまま出す説明。
# ここに無い名前を本文に書くと`unknown_placeholders()`が拾い、プレビューがBLOCKERを出す
# （綴り間違いを「値が空」として黙って通さないため）。
PLACEHOLDERS: dict[str, str] = {
    "会社名": "取引先マスターの「取引先名」",
    "氏名": "連絡先の「名前」",
    "部署": "連絡先の「部署」",
    "役職": "連絡先の「役職」",
    "担当者名": "差出人（この配信を作った営業担当）の名前",
}

# `{{ 会社名 }}`のように前後の空白は許す。改行をまたぐ`{{`は差し込みとみなさない
# （本文に単独で出てくる`{{`が、遠く離れた`}}`と誤って対になるのを防ぐ）。
_PLACEHOLDER_RE = re.compile(r"\{\{[ \t]*([^{}\n]{1,40}?)[ \t]*\}\}")


def find_placeholders(text: str) -> list[str]:
    """本文に出てくる差し込み名を、出現順・重複なしで返す。"""
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def unknown_placeholders(text: str) -> list[str]:
    """`PLACEHOLDERS`に無い差し込み名（＝綴り間違い）を返す。"""
    return [name for name in find_placeholders(text) if name not in PLACEHOLDERS]


@dataclass(frozen=True)
class RenderResult:
    text: str
    # 本文に書かれているのに値が空だった差し込み名（出現順・重複なし）。
    missing: tuple[str, ...]


def render(text: str, values: Mapping[str, str | None]) -> RenderResult:
    """差し込みを1パスで置き換える。

    `values`に無い名前・値が空文字/Noneの名前は空文字に置き換えたうえで`missing`に
    載せる（呼び出し元がその宛先を落とす判断をする）。
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in PLACEHOLDERS:
            # 未知の差し込み名は触らずそのまま残す。プレビューは`unknown_placeholders()`で
            # 別途BLOCKERを出すので、ここで空欄にして見えなくしない方が原因に気づける。
            return match.group(0)
        value = values.get(name)
        if value is None or not str(value).strip():
            if name not in missing:
                missing.append(name)
            return ""
        return str(value)

    return RenderResult(text=_PLACEHOLDER_RE.sub(replace, text or ""), missing=tuple(missing))
