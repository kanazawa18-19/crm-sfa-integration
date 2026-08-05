"""migration配下で共有する小さな純粋関数群。"""

from __future__ import annotations

_PRIMARY_DELIMITER = "、"
_OTHER_DELIMITERS = (",", "，")


def parse_multi_value(value: str | list[str] | None) -> list[str]:
    """kintone/Zohoの複数値項目（サービス名など）を正規化しリストへ分解する。

    kintoneの複数選択はAPI上リストで返ることが多いが、CSV由来やテキスト項目は
    区切り文字入りの文字列で来ることもあるため両対応する（04_項目マッピング
    「提案サービス（テキスト）→ サービス・商品 リレーション：文字列を正規化し分解」）。
    """
    if not value:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]

    text = value
    for delimiter in _OTHER_DELIMITERS:
        text = text.replace(delimiter, _PRIMARY_DELIMITER)
    return [v.strip() for v in text.split(_PRIMARY_DELIMITER) if v.strip()]
