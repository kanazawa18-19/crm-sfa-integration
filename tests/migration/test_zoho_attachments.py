from src.migration.zoho_attachments import (
    DriveFile,
    UnmatchedAttachment,
    build_attachment_groups,
    build_drive_filename_index,
)


def test_build_drive_filename_index_keys_by_name() -> None:
    files = [DriveFile(drive_file_id="abc123", name="g1aoje_見積書.pdf")]

    index = build_drive_filename_index(files)

    assert index == {"g1aoje_見積書.pdf": files[0]}


def test_build_attachment_groups_matches_by_csv_id_and_groups_by_parent() -> None:
    rows = [
        {
            "ID": "g1aoje_見積書A.pdf",
            "ファイル名": "見積書A.pdf",
            "親データID.id": "zcrm_project_1",
        },
        {
            "ID": "kdqib_見積書B.pdf",
            "ファイル名": "見積書B.pdf",
            "親データID.id": "zcrm_project_1",
        },
        {
            "ID": "h86kn_資料C.pdf",
            "ファイル名": "資料C.pdf",
            "親データID.id": "zcrm_project_2",
        },
    ]
    drive_index = build_drive_filename_index(
        [
            DriveFile(drive_file_id="drive-abc", name="g1aoje_見積書A.pdf"),
            DriveFile(drive_file_id="drive-def", name="kdqib_見積書B.pdf"),
            DriveFile(drive_file_id="drive-ghi", name="h86kn_資料C.pdf"),
        ]
    )

    groups, unmatched = build_attachment_groups(rows, drive_index)

    assert unmatched == []
    assert groups["zcrm_project_1"] == [
        {"name": "見積書A.pdf", "url": "https://drive.google.com/file/d/drive-abc/view"},
        {"name": "見積書B.pdf", "url": "https://drive.google.com/file/d/drive-def/view"},
    ]
    assert groups["zcrm_project_2"] == [
        {"name": "資料C.pdf", "url": "https://drive.google.com/file/d/drive-ghi/view"},
    ]


def test_build_attachment_groups_reports_unmatched_when_not_found_in_drive() -> None:
    rows = [{"ID": "missing_file.pdf", "ファイル名": "missing_file.pdf", "親データID.id": "zcrm_1"}]

    groups, unmatched = build_attachment_groups(rows, {})

    assert groups == {}
    assert unmatched == [
        UnmatchedAttachment(csv_id="missing_file.pdf", parent_zoho_id="zcrm_1", file_name="missing_file.pdf")
    ]


def test_build_attachment_groups_matches_despite_unicode_normalization_mismatch() -> None:
    """実データ回帰確認(2026-08-10): Google DriveのファイルはNFD分解形式、Zoho CSVは
    NFC合成形式で、見た目は同一でもバイト表現が異なるケースが実際にあった
    （見積書16件中4件がこれが原因で未マッチだった）。「が」の合成済み文字(NFC)と
    「か」+濁点の分解形式(NFD)を使って再現する。"""
    import unicodedata

    nfc_name = unicodedata.normalize("NFC", "abc123_が.pdf")
    nfd_name = unicodedata.normalize("NFD", "abc123_が.pdf")
    assert nfc_name != nfd_name  # 前提確認: バイト表現として異なることを確認

    drive_index = build_drive_filename_index([DriveFile(drive_file_id="drive-1", name=nfd_name)])
    rows = [{"ID": nfc_name, "ファイル名": "が.pdf", "親データID.id": "zcrm_1"}]

    groups, unmatched = build_attachment_groups(rows, drive_index)

    assert unmatched == []
    assert groups["zcrm_1"] == [
        {"name": "が.pdf", "url": "https://drive.google.com/file/d/drive-1/view"}
    ]


def test_build_attachment_groups_skips_rows_without_id_or_parent() -> None:
    rows = [
        {"ID": "", "ファイル名": "x.pdf", "親データID.id": "zcrm_1"},
        {"ID": "y.pdf", "ファイル名": "y.pdf", "親データID.id": ""},
    ]

    groups, unmatched = build_attachment_groups(rows, {})

    assert groups == {}
    assert unmatched == []
