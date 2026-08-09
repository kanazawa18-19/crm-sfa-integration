"""migration配下で共有する小さな純粋関数群。"""

from __future__ import annotations

import re

_PRIMARY_DELIMITER = "、"
_OTHER_DELIMITERS = (",", "，")

_NOTION_PAGE_ID_RE = re.compile(r"notion\.so/(?:[^/?#]*-)?([0-9a-fA-F]{32})")


def extract_notion_page_id(text: str | None) -> str | None:
    """Zoho側の自由記述/リンク項目に埋め込まれたNotionページURLからページIDを取り出す。

    Zoho実データ確認済み(2026-08-10): 過去の連携作業の名残で、「【Notion】取引先マスター」
    「案件名」等の一部列に`会社名 (https://www.notion.so/xxxxxxxx...?pvs=21)`という形式で
    Notionページへの直リンクが埋め込まれている。これがあれば会社名でのあいまい照合より
    確実な突合キーとして使える。ハイフン区切りのスラグ付きURL
    （`https://www.notion.so/slug-xxxxxxxx...`）にも対応する。
    """
    if not text:
        return None
    match = _NOTION_PAGE_ID_RE.search(text)
    if not match:
        return None
    return match.group(1)


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
