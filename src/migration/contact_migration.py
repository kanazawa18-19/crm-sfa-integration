"""kintone 取引先マスタ「担当者名1〜3」の縦持ち化と、Q-08名寄せロジック。

Q-08（10_保留・要確認事項）: メールアドレスを一意キーとする。
メール未登録者は氏名＋取引先名で突合する。
"""

from __future__ import annotations

_MAX_CONTACT_SLOTS = 3


def split_kintone_contacts(record: dict[str, str]) -> list[dict[str, str | None]]:
    """kintone 取引先マスタの担当者名1〜3（横持ち）を1人1レコードの連絡先へ分割する。"""
    company_name = record.get("顧客名（法人・個人・施設）", "")
    kintone_client_id = record.get("レコード番号", "")

    contacts: list[dict[str, str | None]] = []
    for slot in range(1, _MAX_CONTACT_SLOTS + 1):
        name = (record.get(f"担当者名{slot}") or "").strip()
        if not name:
            continue
        contacts.append(
            {
                "氏名": name,
                "部署": (record.get(f"部署{slot}") or "").strip() or None,
                "役職": (record.get(f"役職{slot}") or "").strip() or None,
                "携帯番号": (record.get(f"携帯{slot}") or "").strip() or None,
                "メールアドレス": (record.get(f"メール{slot}") or "").strip() or None,
                "取引先名": company_name,
                "kintone_client_id": kintone_client_id,
            }
        )
    return contacts


def build_contact_dedup_key(contact: dict[str, str | None]) -> str:
    """名寄せキーを生成する。メールアドレスがあればそれを優先し、無ければ氏名＋取引先名。"""
    email = (contact.get("メールアドレス") or "").strip().lower()
    if email:
        return f"email:{email}"
    name = (contact.get("氏名") or "").strip()
    client_name = (contact.get("取引先名") or "").strip()
    return f"name_client:{name}|{client_name}"


def dedupe_contacts(contacts: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    """同一人物とみなせる連絡先を名寄せキーでマージする。

    先に現れたレコードを基本形とし、後続の同一キーレコードが持つ非空項目のみで補完する
    （既存値は上書きしない）。
    """
    merged: dict[str, dict[str, str | None]] = {}
    order: list[str] = []
    for contact in contacts:
        key = build_contact_dedup_key(contact)
        if key not in merged:
            merged[key] = dict(contact)
            order.append(key)
            continue
        for field, value in contact.items():
            if value and not merged[key].get(field):
                merged[key][field] = value
    return [merged[key] for key in order]
