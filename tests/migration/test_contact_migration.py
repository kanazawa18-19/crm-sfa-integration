from src.migration.contact_migration import (
    build_contact_dedup_key,
    dedupe_contacts,
    split_kintone_contacts,
)


def test_split_kintone_contacts_extracts_all_populated_slots() -> None:
    record = {
        "レコード番号": "2001",
        "顧客名（法人・個人・施設）": "株式会社サンプル",
        "担当者名1": "山田太郎",
        "部署1": "営業部",
        "役職1": "部長",
        "携帯1": "090-1111-1111",
        "メール1": "yamada@example.com",
        "担当者名2": "鈴木花子",
        "部署2": "経理部",
        "役職2": "",
        "携帯2": "",
        "メール2": "",
        "担当者名3": "",
    }

    contacts = split_kintone_contacts(record)

    assert len(contacts) == 2
    assert contacts[0] == {
        "氏名": "山田太郎",
        "部署": "営業部",
        "役職": "部長",
        "携帯番号": "090-1111-1111",
        "メールアドレス": "yamada@example.com",
        "取引先名": "株式会社サンプル",
        "kintone_client_id": "2001",
    }
    assert contacts[1]["氏名"] == "鈴木花子"
    assert contacts[1]["役職"] is None
    assert contacts[1]["メールアドレス"] is None


def test_split_kintone_contacts_skips_empty_slots() -> None:
    record = {"レコード番号": "2002", "顧客名（法人・個人・施設）": "株式会社サンプル"}

    assert split_kintone_contacts(record) == []


def test_build_contact_dedup_key_prefers_email() -> None:
    contact = {"氏名": "山田太郎", "メールアドレス": "Yamada@Example.com", "取引先名": "株式会社サンプル"}

    assert build_contact_dedup_key(contact) == "email:yamada@example.com"


def test_build_contact_dedup_key_falls_back_to_name_and_client() -> None:
    contact = {"氏名": "山田太郎", "メールアドレス": None, "取引先名": "株式会社サンプル"}

    assert build_contact_dedup_key(contact) == "name_client:山田太郎|株式会社サンプル"


def test_dedupe_contacts_merges_by_email_and_fills_missing_fields() -> None:
    contacts = [
        {
            "氏名": "山田太郎",
            "部署": None,
            "役職": "部長",
            "携帯番号": None,
            "メールアドレス": "yamada@example.com",
            "取引先名": "株式会社サンプル",
        },
        {
            "氏名": "山田太郎",
            "部署": "営業部",
            "役職": "課長",
            "携帯番号": "090-1111-1111",
            "メールアドレス": "yamada@example.com",
            "取引先名": "株式会社サンプル",
        },
    ]

    result = dedupe_contacts(contacts)

    assert len(result) == 1
    assert result[0]["部署"] == "営業部"
    assert result[0]["役職"] == "部長"
    assert result[0]["携帯番号"] == "090-1111-1111"


def test_dedupe_contacts_keeps_distinct_people_separate() -> None:
    contacts = [
        {"氏名": "山田太郎", "メールアドレス": "yamada@example.com", "取引先名": "A社"},
        {"氏名": "山田太郎", "メールアドレス": None, "取引先名": "B社"},
    ]

    result = dedupe_contacts(contacts)

    assert len(result) == 2


def test_dedupe_contacts_no_email_same_name_different_client_stays_separate() -> None:
    """Q-08: メール未登録者は氏名＋取引先名で突合する。

    両方ともメールアドレス無しで氏名が同一でも、取引先名が異なれば
    別人（別レコード）として名寄せされずに残ることを直接検証する。
    """
    contacts = [
        {"氏名": "山田太郎", "メールアドレス": None, "取引先名": "A社", "部署": "営業部"},
        {"氏名": "山田太郎", "メールアドレス": None, "取引先名": "B社", "部署": "総務部"},
    ]

    keys = {build_contact_dedup_key(c) for c in contacts}
    assert keys == {"name_client:山田太郎|A社", "name_client:山田太郎|B社"}

    result = dedupe_contacts(contacts)

    assert len(result) == 2
    result_by_client = {c["取引先名"]: c for c in result}
    assert result_by_client["A社"]["部署"] == "営業部"
    assert result_by_client["B社"]["部署"] == "総務部"
