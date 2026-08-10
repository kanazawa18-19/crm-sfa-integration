"""Zoho添付ファイル（見積書/申込書・契約書・受注書/個別提案資料/手当情報アップロード）→
案件管理DB・アクション履歴DBのFILES型プロパティへの紐付けロジック。

実データ確認済み（2026-08-10）:
- Google Driveの共有フォルダ「Attachments」には825件の実ファイルが格納されており、
  対象4つのZoho添付系CSV（見積書16件＋申込書／契約書／受注書42件＋個別提案資料5件＋
  手当情報アップロード762件＝825件）の合計件数と完全に一致することを確認済み。
- 各CSVの「ID」列の値（例:
  "g1aoje8e7b9b7ab1d473ca8a58db9d0cc4f54_【リピッテホテルお見積書...】.pdf"）は、
  Google Drive上の実ファイル名と完全に一致する（`{File Id}_{元のファイル名}`形式）。
  そのためDriveのファイル名からCSVの「ID」列への単純な文字列一致で紐付けできる。
- 各CSVの「親データID.id」列が、紐付け先（案件 or アクション）のZoho データIDを指す。

Notion側のFILES型プロパティはNotionにファイル本体をアップロードするのではなく、
Google Driveへの外部リンク（`https://drive.google.com/file/d/{fileId}/view`）として
登録する方式を取る（build_notion_property_value()のFILES型対応、2026-08-10追加）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

_DRIVE_VIEW_URL_TEMPLATE = "https://drive.google.com/file/d/{file_id}/view"


def _normalize_filename(name: str) -> str:
    """ファイル名をNFC形式へ正規化する。

    実データ確認済み(2026-08-10): Google Driveのファイル名（macOS経由のアップロード等で
    NFD分解形式になりがち）と、ZohoのCSVエクスポートのファイル名（NFC合成形式）とで
    Unicode正規化形式が異なり、見た目は同一でも単純な文字列比較では一致しないケースが
    実際にあった（例: 見積書CSVの16件中4件がこれが原因で未マッチになっていた）。
    """
    return unicodedata.normalize("NFC", name)


@dataclass(frozen=True)
class DriveFile:
    """Google Drive「Attachments」フォルダ内の1ファイル。"""

    drive_file_id: str
    name: str


def build_drive_filename_index(drive_files: list[DriveFile]) -> dict[str, DriveFile]:
    """Driveのファイル名（NFC正規化済み）をキーにした検索用インデックスを構築する。"""
    return {_normalize_filename(f.name): f for f in drive_files}


def _notion_file_ref(drive_file: DriveFile, *, display_name: str) -> dict[str, str]:
    return {
        "name": display_name,
        "url": _DRIVE_VIEW_URL_TEMPLATE.format(file_id=drive_file.drive_file_id),
    }


@dataclass(frozen=True)
class UnmatchedAttachment:
    """CSVには存在するが、Driveのファイル一覧に見つからなかった添付ファイル
    （名寄せ漏れの可視化用）。"""

    csv_id: str
    parent_zoho_id: str
    file_name: str


def build_attachment_groups(
    rows: list[dict[str, str]],
    drive_index: dict[str, DriveFile],
) -> tuple[dict[str, list[dict[str, str]]], list[UnmatchedAttachment]]:
    """添付ファイルCSVの行群を、親レコード（案件 or アクション）のZoho データIDごとに
    Notion FILES型プロパティ値のリストへグループ化する。

    戻り値は (親データID -> ファイル参照のリスト, Driveで見つからなかった添付の一覧)。
    """
    groups: dict[str, list[dict[str, str]]] = {}
    unmatched: list[UnmatchedAttachment] = []
    for row in rows:
        csv_id = row.get("ID") or ""
        parent_id = row.get("親データID.id") or ""
        file_name = row.get("ファイル名") or csv_id
        if not csv_id or not parent_id:
            continue
        drive_file = drive_index.get(_normalize_filename(csv_id))
        if drive_file is None:
            unmatched.append(
                UnmatchedAttachment(csv_id=csv_id, parent_zoho_id=parent_id, file_name=file_name)
            )
            continue
        groups.setdefault(parent_id, []).append(
            _notion_file_ref(drive_file, display_name=file_name)
        )
    return groups, unmatched
