"""migration配下で共有する小さな純粋関数群。"""

from __future__ import annotations

import datetime
import logging
import re

logger = logging.getLogger(__name__)

_PRIMARY_DELIMITER = "、"
_OTHER_DELIMITERS = (",", "，")

_NOTION_PAGE_ID_RE = re.compile(r"notion\.so/(?:[^/?#]*-)?([0-9a-fA-F]{32})")

# 末尾アンカー無し（時刻付きISO 8601、例: "2024-05-10T12:00:00.000Z"も許容するため）。
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T.*)?$")
_DATE_KANJI_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")
_DATE_SLASH_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


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


def normalize_date(raw: str | None) -> str | None:
    """kintone/ZohoのCSV由来の日付文字列をNotion DATEプロパティが要求するISO 8601
    （`YYYY-MM-DD`）へ正規化する。

    実データ確認済み（2026-08-11、本番移行の`契約日 / 予想契約日`書き込みでNotion API
    から`HTTP 400: ... should be a valid ISO 8601 date string`が返り判明）:
    kintone CSVは`2023/12/01`（スラッシュ区切り）、Zoho CSVは`2024年5月10日`
    （漢字区切り）で日付を出力しており、いずれもNotion APIにそのまま渡すと拒否される。
    既にISO 8601（`YYYY-MM-DD...`、末尾に時刻が付く場合も含む）ならそのまま返す。
    どの形式にも一致しない場合は、移行全体を止めるより値を捨てる方が安全なため
    Noneを返す（warningログに残し、後から実データ精査で気づけるようにする）。
    """
    if not raw:
        return None
    text = raw.strip()
    if _DATE_ISO_RE.match(text):
        return text
    match = _DATE_KANJI_RE.match(text) or _DATE_SLASH_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            # shirokuma-secレビューWARN対応（2026-08-11）: 正規表現は桁数のみ検証し
            # 月13・日40等の暦上あり得ない値もそのまま素通ししていた。datetime.date()で
            # 実在する暦日かどうかを検証し、不正ならISO"形式もどき"の壊れた文字列を
            # 返さずNoneへフォールバックする（Notion APIへ送ればどのみち400になるだけの
            # 無効な値を作り出さないため）。
            return datetime.date(year, month, day).isoformat()
        except ValueError:
            logger.warning("normalize_date: 暦日として不正なため値を破棄しました: %r", raw)
            return None
    logger.warning("normalize_date: 未知の日付形式のため値を破棄しました: %r", raw)
    return None


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
