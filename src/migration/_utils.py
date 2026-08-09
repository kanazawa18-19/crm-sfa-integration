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


def parse_checkbox_columns(record: dict[str, str], *, prefix: str) -> list[str]:
    """kintoneのチェックボックス項目（複数選択）をCSVエクスポートした形式を分解する。

    kintoneはチェックボックス項目をCSV出力する際、1項目を`{prefix}[選択肢名]`という
    列名の集合に展開し、チェック済みの列にのみ値（"1"）を入れる仕様
    （実データ確認済み: 例えば案件管理の「サービス（ランニング）」は
    「サービス（ランニング）[ホテラボ]」「サービス（ランニング）[メイリー]」...という
    複数列に分かれており、単一の「サービス（ランニング）」列は存在しない）。
    `record`から`{prefix}[`で始まり`]`で終わる列を全て走査し、値が空でない（チェック済み）
    選択肢名のリストを返す。
    """
    prefix_bracket = f"{prefix}["
    selected: list[str] = []
    for key, value in record.items():
        if not key.startswith(prefix_bracket) or not key.endswith("]"):
            continue
        if value and value.strip():
            option = key[len(prefix_bracket) : -1]
            selected.append(option)
    return selected
