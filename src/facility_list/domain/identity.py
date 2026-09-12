"""施設と取引先の二次照合キー。外部サービスを使わない保守的な正規化。"""

from __future__ import annotations

import re
import unicodedata


def normalize_phone(value: str | None) -> str | None:
    """国内番号と+81表記を揃える。内線・複数番号・説明付きは推測しない。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = re.sub(r"[\s()‐‑‒–—―−ー-]", "", text)
    if text.startswith("+81"):
        text = "0" + text[3:].removeprefix("0")
    if not re.fullmatch(r"0[1-9][0-9]{8,9}", text):
        return None
    return text


def normalize_address(value: str | None, prefecture: str | None = None) -> str | None:
    """番地まである住所の表記を揃える。建物・階・部屋を削らず別所在地を混ぜない。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = re.sub(r"^〒?\s*[0-9]{3}[-‐−ー]?[0-9]{4}\s*", "", text)
    text = re.sub(r"\s+", "", text)
    pref = unicodedata.normalize("NFKC", prefecture or "").strip()
    # CRMの都道府県欄には市区町村等もあるため、正式な接尾辞を持つものだけ補う。
    if not re.match(r"^(?:北海道|東京都|京都府|大阪府|.{2,3}県)", text):
        if not re.fullmatch(r"北海道|東京都|京都府|大阪府|.{2,3}県", pref):
            return None
        text = pref + text
    # 市区町村まで／番地不明の住所を同一施設の証拠にしない。
    if not re.search(r"[市区町村].*[0-9]", text):
        return None
    text = re.sub(r"(?<=[0-9])[‐‑‒–—―−ー](?=[0-9])", "-", text)
    text = re.sub(r"(?<=[0-9])(?:丁目|番地の|番地|番|号)(?=[0-9])", "-", text)
    text = re.sub(r"(?<=[0-9])(?:番地|番|号)$", "", text)
    return text
