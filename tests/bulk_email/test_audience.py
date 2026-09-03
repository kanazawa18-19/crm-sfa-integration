"""宛先の抽出・除外の検証（2026-09-03）。

`select_recipients`は**送ってよい根拠が無い相手を既定で外す**ので、
「送れること」を確かめるテストは必ず`consents=`を渡す。渡し忘れると
全員が`consent_missing`で外れる（それが仕様）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.bulk_email.audience import (
    SKIP_DUPLICATE,
    SKIP_INVALID_EMAIL,
    SKIP_NO_EMAIL,
    SKIP_UNSUBSCRIBED,
    Contact,
    is_valid_email,
    normalize_email,
    select_recipients,
)
from src.bulk_email.consent import (
    BASIS_NOTIFIED,
    REASON_MISSING,
    REASON_NO_DATE,
    REASON_REVOKED,
    REASON_UNKNOWN_BASIS,
    STALE_AFTER_DAYS,
    ConsentRecord,
)

NOW = datetime(2026, 9, 3, 6, tzinfo=timezone.utc)  # JST 9/3 15:00


def contact(page_id: str, email: str | None, name: str = "山田") -> Contact:
    return Contact(page_id=page_id, name=name, email=email, client_name="テスト商事")


def allow(*page_ids: str, days_ago: int = 30, email: str | None = None, **kwargs: object):
    """指定した連絡先に「送ってよい根拠」が登録されている状態を作る。

    根拠は**登録時のアドレスと今の宛先が一致したときだけ**効くので、
    `contact()`が作るアドレス（`<page_id>@example.com`ではなく引数で渡した値）に
    合わせる必要がある。既定は`a@example.com`。
    """
    values: dict = {
        "basis": BASIS_NOTIFIED,
        "obtained_at": NOW.date() - timedelta(days=days_ago),
        "evidence": "2026-08 展示会で名刺交換",
    }
    values.update(kwargs)
    return [
        ConsentRecord(contact_page_id=page_id, contact_email=email or "a@example.com", **values)
        for page_id in page_ids
    ]


def test_送れる宛先だけが残る() -> None:
    recipients, skipped, stale = select_recipients(
        [contact("p1", "a@example.com")], consents=allow("p1"), now=NOW
    )
    assert [c.page_id for c in recipients] == ["p1"]
    assert skipped == []
    assert stale == []


def test_配信停止はページIDでもアドレスでも外れる() -> None:
    contacts = [contact("p1", "a@example.com"), contact("p2", "b@example.com")]
    recipients, skipped, _ = select_recipients(
        contacts,
        opted_out_page_ids=["p1"],
        opted_out_emails=["B@EXAMPLE.COM"],
        consents=allow("p1", "p2"),
        now=NOW,
    )
    assert recipients == []
    assert {s.reason for s in skipped} == {SKIP_UNSUBSCRIBED}


def test_配信停止はアドレス不正より優先して判定される() -> None:
    """形式を直したら対象へ戻ってしまう、という事故を防ぐ。"""
    _, skipped, _ = select_recipients(
        [contact("p1", "こわれた")], opted_out_page_ids=["p1"], consents=allow("p1"), now=NOW
    )
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]


def test_アドレス未登録と形式不正を分けて返す() -> None:
    _, skipped, _ = select_recipients(
        [contact("p1", None), contact("p2", "a@b")], consents=allow("p1", "p2"), now=NOW
    )
    assert [s.reason for s in skipped] == [SKIP_NO_EMAIL, SKIP_INVALID_EMAIL]


def test_1つの欄に複数アドレスが入っていたら送らない() -> None:
    """`a@x.jp, b@y.jp`のような実データを勝手に分割・採用しない。"""
    recipients, skipped, _ = select_recipients(
        [contact("p1", "a@x.jp, b@y.jp")], consents=allow("p1"), now=NOW
    )
    assert recipients == []
    assert skipped[0].reason == SKIP_INVALID_EMAIL
    # 何が入っていたかを画面に出せること（人が直す対象なので値を隠さない）。
    assert skipped[0].detail == "a@x.jp, b@y.jp"


def test_同じアドレスは先に出てきた1件だけ残す() -> None:
    recipients, skipped, _ = select_recipients(
        [contact("p1", "a@example.com"), contact("p2", "A@Example.com ")],
        consents=allow("p1", "p2"),
        now=NOW,
    )
    assert [c.page_id for c in recipients] == ["p1"]
    assert [s.reason for s in skipped] == [SKIP_DUPLICATE]


def test_同じ連絡先が2回入ってきても1件だけ残す() -> None:
    recipients, skipped, _ = select_recipients(
        [contact("p1", "a@example.com")] * 2, consents=allow("p1"), now=NOW
    )
    assert len(recipients) == 1
    assert [s.reason for s in skipped] == [SKIP_DUPLICATE]


def test_除外理由には日本語のラベルが付く() -> None:
    _, skipped, _ = select_recipients([contact("p1", None)], consents=allow("p1"), now=NOW)
    assert skipped[0].reason_label == "メールアドレスが未登録"


def test_アドレスの正規化() -> None:
    assert normalize_email("  A@Example.COM ") == "a@example.com"
    assert normalize_email(None) == ""


def test_アドレスの形式判定() -> None:
    assert is_valid_email("a@example.com")
    assert not is_valid_email("a@b")
    assert not is_valid_email("a@example.com>")
    assert not is_valid_email("a b@example.com")


def test_ページIDの前後空白で配信停止がすり抜けない() -> None:
    """除外リスト側だけ空白を落としていると、空白1つで「停止済みなのに送れる宛先」になる。"""
    recipients, skipped, _ = select_recipients(
        [contact(" p1 ", "a@example.com")],
        opted_out_page_ids=["p1"],
        consents=allow("p1"),
        now=NOW,
    )
    assert recipients == []
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]


def test_ページIDの表記ゆれで配信停止がすり抜けない() -> None:
    """DB側は正規化済み（ハイフン無し）、Notion側はハイフン付きで来る。

    突合を正規化した形で行っていないと、同じ人が別人として通ってしまう。
    """
    recipients, skipped, _ = select_recipients(
        [contact("3CED8EA8-1234", "a@example.com")],
        opted_out_page_ids=["3ced8ea81234"],
        consents=allow("3ced8ea81234"),
        now=NOW,
    )
    assert recipients == []
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]


# ── 送ってよい根拠（`src/bulk_email/consent.py`）との接続 ──────────────────


def test_根拠が無い相手には送らない() -> None:
    """既定は送信不可。配信停止の名簿に載っていないことは、送ってよい理由ではない。"""
    recipients, skipped, _ = select_recipients([contact("p1", "a@example.com")], now=NOW)
    assert recipients == []
    assert [s.reason for s in skipped] == [REASON_MISSING]
    assert skipped[0].reason_label == "送ってよい根拠が未登録"


def test_取り消された根拠では送らない() -> None:
    consents = allow("p1", revoked_at=NOW - timedelta(days=1))
    _, skipped, _ = select_recipients([contact("p1", "a@example.com")], consents=consents, now=NOW)
    assert [s.reason for s in skipped] == [REASON_REVOKED]


def test_種類が不明な根拠では送らない() -> None:
    """コードの定義を変えたのに古い行が残っている、を素通りさせない。"""
    consents = allow("p1", basis="むかしの種類")
    _, skipped, _ = select_recipients([contact("p1", "a@example.com")], consents=consents, now=NOW)
    assert [s.reason for s in skipped] == [REASON_UNKNOWN_BASIS]


def test_取得日が無い根拠では送らない() -> None:
    consents = allow("p1", obtained_at=None)
    _, skipped, _ = select_recipients([contact("p1", "a@example.com")], consents=consents, now=NOW)
    assert [s.reason for s in skipped] == [REASON_NO_DATE]


def test_根拠が古くても送れるが分けて返す() -> None:
    """古さは機械が決められる話ではないので、止めずに件数だけ出す。"""
    consents = allow("p1", days_ago=STALE_AFTER_DAYS + 1)
    recipients, skipped, stale = select_recipients(
        [contact("p1", "a@example.com")], consents=consents, now=NOW
    )
    assert [c.page_id for c in recipients] == ["p1"]
    assert skipped == []
    assert [c.page_id for c in stale] == ["p1"]


def test_配信停止は根拠より優先される() -> None:
    """根拠を登録しても、停止の申し出があれば送らない。"""
    _, skipped, _ = select_recipients(
        [contact("p1", "a@example.com")],
        opted_out_page_ids=["p1"],
        consents=allow("p1"),
        now=NOW,
    )
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]


def test_同じアドレスの連絡先は根拠がある方が残る() -> None:
    """根拠の無い1件目が「重複」の枠を取って、根拠のある2件目まで落とさない。"""
    consents = allow("p2")
    recipients, skipped, _ = select_recipients(
        [contact("p1", "a@example.com"), contact("p2", "a@example.com")],
        consents=consents,
        now=NOW,
    )
    assert [c.page_id for c in recipients] == ["p2"]
    assert [(s.contact.page_id, s.reason) for s in skipped] == [("p1", REASON_MISSING)]
