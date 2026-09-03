"""宛先の抽出・除外の検証（2026-09-03）。"""

from __future__ import annotations

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


def contact(page_id: str, email: str | None, name: str = "山田") -> Contact:
    return Contact(page_id=page_id, name=name, email=email, client_name="テスト商事")


def test_送れる宛先だけが残る() -> None:
    recipients, skipped = select_recipients([contact("p1", "a@example.com")])
    assert [c.page_id for c in recipients] == ["p1"]
    assert skipped == []


def test_配信停止はページIDでもアドレスでも外れる() -> None:
    contacts = [contact("p1", "a@example.com"), contact("p2", "b@example.com")]
    recipients, skipped = select_recipients(
        contacts, opted_out_page_ids=["p1"], opted_out_emails=["B@EXAMPLE.COM"]
    )
    assert recipients == []
    assert {s.reason for s in skipped} == {SKIP_UNSUBSCRIBED}


def test_配信停止はアドレス不正より優先して判定される() -> None:
    """形式を直したら対象へ戻ってしまう、という事故を防ぐ。"""
    _, skipped = select_recipients([contact("p1", "こわれた")], opted_out_page_ids=["p1"])
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]


def test_アドレス未登録と形式不正を分けて返す() -> None:
    _, skipped = select_recipients([contact("p1", None), contact("p2", "a@b")])
    assert [s.reason for s in skipped] == [SKIP_NO_EMAIL, SKIP_INVALID_EMAIL]


def test_1つの欄に複数アドレスが入っていたら送らない() -> None:
    """`a@x.jp, b@y.jp`のような実データを勝手に分割・採用しない。"""
    recipients, skipped = select_recipients([contact("p1", "a@x.jp, b@y.jp")])
    assert recipients == []
    assert skipped[0].reason == SKIP_INVALID_EMAIL
    # 何が入っていたかを画面に出せること（人が直す対象なので値を隠さない）。
    assert skipped[0].detail == "a@x.jp, b@y.jp"


def test_同じアドレスは先に出てきた1件だけ残す() -> None:
    recipients, skipped = select_recipients(
        [contact("p1", "a@example.com"), contact("p2", "A@Example.com ")]
    )
    assert [c.page_id for c in recipients] == ["p1"]
    assert [s.reason for s in skipped] == [SKIP_DUPLICATE]


def test_同じ連絡先が2回入ってきても1件だけ残す() -> None:
    recipients, skipped = select_recipients([contact("p1", "a@example.com")] * 2)
    assert len(recipients) == 1
    assert [s.reason for s in skipped] == [SKIP_DUPLICATE]


def test_除外理由には日本語のラベルが付く() -> None:
    _, skipped = select_recipients([contact("p1", None)])
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
    recipients, skipped = select_recipients(
        [contact(" p1 ", "a@example.com")], opted_out_page_ids=["p1"]
    )
    assert recipients == []
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]


def test_ページIDの表記ゆれで配信停止がすり抜けない() -> None:
    """DB側は正規化済み（ハイフン無し）、Notion側はハイフン付きで来る。

    突合を正規化した形で行っていないと、同じ人が別人として通ってしまう。
    """
    recipients, skipped = select_recipients(
        [contact("3CED8EA8-1234", "a@example.com")],
        opted_out_page_ids=["3ced8ea81234"],
    )
    assert recipients == []
    assert [s.reason for s in skipped] == [SKIP_UNSUBSCRIBED]
