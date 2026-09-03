"""宛先の抽出と除外（純粋関数のみ、2026-09-03）。

一斉配信で一番怖いのは「送ってはいけない相手に送ること」なので、
**除外は必ずここを通す**。理由を必ず添えて返し、画面に全部出す
（黙って減った宛先ほど気づけないものはない）。

```
   連絡先の一覧 ──▶ select_recipients() ──┬──▶ 送れる宛先
   （Notion）        配信停止フラグ        └──▶ 外した宛先 ＋ 理由
```

判定の順番には意味がある。**配信停止を最優先で見る。**
アドレスの形が変でも、停止の申し出があった相手は「停止で外した」と記録したい
（形式不正として扱うと、直したときにまた対象へ戻ってしまう）。

■ 送ってよい根拠（オプトイン）が無い相手は、既定で外す

```
   ① 配信停止の申し出がある      ──▶ 外す（最優先）
   ② アドレスが無い／形が不正     ──▶ 外す
   ③ 同じアドレスが重複          ──▶ 外す
   ④ 送ってよい根拠が無い        ──▶ 外す  ★ `src/bulk_email/consent.py`
```

④を最後に見るのは、①〜③の方が「何を直せばよいか」がはっきりしているため
（根拠が無いことは、アドレスが壊れていることの言い訳にはならない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from src.bulk_email import consent as consent_module
from src.bulk_email.consent import ConsentIndex, ConsentRecord
from src.bulk_email.ids import normalize_page_id

# 除外の理由（コード）と、画面に出す日本語。
SKIP_UNSUBSCRIBED = "unsubscribed"
SKIP_NO_EMAIL = "no_email"
SKIP_INVALID_EMAIL = "invalid_email"
SKIP_DUPLICATE = "duplicate"
SKIP_MISSING_MERGE_VALUE = "missing_merge_value"

SKIP_REASON_LABELS: dict[str, str] = {
    SKIP_UNSUBSCRIBED: "配信停止の申し出あり",
    SKIP_NO_EMAIL: "メールアドレスが未登録",
    SKIP_INVALID_EMAIL: "メールアドレスの形式が不正",
    SKIP_DUPLICATE: "同じアドレスが他の連絡先と重複",
    SKIP_MISSING_MERGE_VALUE: "差し込む値が空",
    # 送ってよい根拠まわり（`src/bulk_email/consent.py`が理由コードの正本）。
    **consent_module.REASON_LABELS,
}

# 「送ってよい根拠が無い／使えない」に当たる理由コード。画面での案内の出し分けに使う。
CONSENT_SKIP_REASONS = frozenset(consent_module.REASON_LABELS)

# 保守的に見る。1つのフィールドに複数アドレスが入っている（`a@x.jp, b@y.jp`）ケースは
# CRMの実データでは珍しくないが、勝手に分割も採用もしない。人が直す対象として返す。
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


@dataclass(frozen=True)
class Contact:
    """Notion連絡先DBの1件（一斉配信が使う項目だけに絞った形）。"""

    page_id: str
    name: str
    email: str | None
    department: str | None = None
    title: str | None = None
    client_name: str = ""
    # どの取引先からぶら下がってきたか。送信の判断には使わないが、
    # 根拠の登録画面が「この連絡先はどの取引先の下にいるか」を持ち回るために要る
    # （名前で逆引きすると同名の取引先で取り違える。3体が独立に指摘、2026-09-03）。
    client_page_id: str = ""


@dataclass(frozen=True)
class SkippedContact:
    contact: Contact
    reason: str
    # 何が原因だったかの具体（不正だったアドレス、空だった差し込み名など）。
    detail: str = ""

    @property
    def reason_label(self) -> str:
        return SKIP_REASON_LABELS.get(self.reason, self.reason)


def normalize_email(value: str | None) -> str:
    """比較・重複判定に使う形（前後の空白を落として小文字）。"""
    return (value or "").strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def select_recipients(
    contacts: Sequence[Contact],
    *,
    opted_out_page_ids: Iterable[str] = (),
    opted_out_emails: Iterable[str] = (),
    consents: Iterable[ConsentRecord] = (),
    now: datetime | None = None,
) -> tuple[list[Contact], list[SkippedContact], list[Contact]]:
    """送れる宛先と、外した宛先（理由つき）と、根拠が古い宛先に分ける。

    同じアドレスが複数の連絡先に登録されている場合は**先に出てきた1件だけ**を残す。
    1人に同じ内容が2通届くのは、営業のメールとしては失礼にあたるだけでなく、
    受信側のスパム判定を悪化させる。

    `consents`を渡さないと**全員が「根拠が未登録」で外れる**。これは仕様。
    引数を足し忘れた送信コードが、根拠を確かめずに送ってしまう方が危ないため、
    「渡し忘れたら送れない」側に倒してある。
    """
    # 突合は必ず正規化した形で行う（`src/bulk_email/ids.py`）。
    # 「db.pyが元の表記へ戻してくれるから、ここは生の値の比較でよい」にすると、
    # 将来この関数を直接呼ぶ送信コードが書かれた瞬間に
    # `abc-def…` != `abcdef…` で配信停止がすり抜ける（ChatGPTレビュー指摘、2026-09-03）。
    opted_out_ids = {normalize_page_id(page_id) for page_id in opted_out_page_ids}
    opted_out_ids.discard("")
    opted_out_addresses = {normalize_email(email) for email in opted_out_emails}
    opted_out_addresses.discard("")

    consent_index = ConsentIndex(consents)

    recipients: list[Contact] = []
    skipped: list[SkippedContact] = []
    # 送れるが根拠が古い宛先。送信は止めないが、件数を画面へ出すために分けて返す。
    stale_consent: list[Contact] = []
    seen_emails: set[str] = set()
    seen_page_ids: set[str] = set()

    for contact in contacts:
        email = normalize_email(contact.email)
        # 除外リストと同じ正規化を通してから比べる（表記ゆれ1つで
        # 「停止済みなのに送れる宛先」に化けるため）。
        page_id = normalize_page_id(contact.page_id)

        # 同じ連絡先が2回入ってきた場合（複数の取引先を選んで、同じ人がぶら下がっていた等）。
        if page_id in seen_page_ids:
            skipped.append(SkippedContact(contact, SKIP_DUPLICATE, "同じ連絡先が重複"))
            continue
        seen_page_ids.add(page_id)

        # 配信停止が最優先（モジュールdocstring参照）。
        if page_id in opted_out_ids or (email and email in opted_out_addresses):
            skipped.append(SkippedContact(contact, SKIP_UNSUBSCRIBED))
            continue

        if not email:
            skipped.append(SkippedContact(contact, SKIP_NO_EMAIL))
            continue

        if not is_valid_email(email):
            skipped.append(SkippedContact(contact, SKIP_INVALID_EMAIL, contact.email or ""))
            continue

        if email in seen_emails:
            skipped.append(SkippedContact(contact, SKIP_DUPLICATE, email))
            continue

        # 最後に「そもそも送ってよい相手か」を見る。
        # 配信停止＝送ってはいけない人の名簿を通過しただけでは、送ってよい理由にならない。
        decision = consent_index.decide(contact.page_id, email, now=now)
        if not decision.allowed:
            # **ここで seen_emails に入れない。** 同じアドレスの連絡先が2件あり、
            # 根拠が付いているのが後ろの1件だけ、というときに前の1件が場所を取ると
            # 後ろが「重複」で外れ、誰にも送られなくなる。
            skipped.append(SkippedContact(contact, decision.reason, decision.detail))
            continue
        if decision.stale:
            stale_consent.append(contact)

        seen_emails.add(email)
        recipients.append(contact)

    return recipients, skipped, stale_consent
