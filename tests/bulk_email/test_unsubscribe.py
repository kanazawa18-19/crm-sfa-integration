"""配信停止リンクの署名の検証（2026-09-03）。"""

from __future__ import annotations

import pytest

from src.bulk_email.ids import normalize_page_id
from src.bulk_email.unsubscribe import (
    build_token,
    build_unsubscribe_url,
    load_secret,
    verify_token,
)

SECRET = "test-secret"
PAGE_ID = "3ced8ea8-1234-814a-83ce-cb3645539acd"


def test_ページIDの正規化() -> None:
    assert normalize_page_id(" 3CED8EA8-1234 ") == "3ced8ea81234"
    assert normalize_page_id("") == ""


# Python側とTypeScript側で同じ値になることを固定するための既知の値。
# `dashboard/lib/__tests__/bulkEmailUnsubscribe.test.ts`にも同じ値が置いてある。
#
# **両側に置くことに意味がある。** 片方にしか無いと、
#   TS側を変えた   → TS側のテストが落ちる（検知できる）
#   Python側を変えた → どちらのテストも緑のまま（検知できない）
# となり、「本文には配信停止リンクが載っているのに開いても止まらない」という、
# 送った後にしか気づけない壊れ方をする（kuma-qaレビューBLOCKER、2026-09-03）。
KNOWN_TOKEN = "Uhektz3Z2HK2TAPzbDZbnQvwX-Q3uen65--WCUTHyIc"


def test_署名は既知の値と一致する() -> None:
    """アルゴリズムを変えたらTypeScript側も必ず直す、を固定する。"""
    assert build_token(SECRET, PAGE_ID) == KNOWN_TOKEN


def test_発行した署名は検証を通る() -> None:
    assert verify_token(SECRET, PAGE_ID, build_token(SECRET, PAGE_ID))


def test_空のページIDでも例外にならない() -> None:
    """壊れたURLを開いただけで500にしない。署名としては通らないことも確かめる。"""
    assert build_token(SECRET, "")
    assert not verify_token(SECRET, "", build_token(SECRET, PAGE_ID))


def test_ハイフンの有無で署名が変わらない() -> None:
    """Python側が発行したリンクを、別の表記のIDで検証しても通ること。"""
    assert build_token(SECRET, PAGE_ID) == build_token(SECRET, PAGE_ID.replace("-", "").upper())


def test_鍵が違えば通らない() -> None:
    assert not verify_token("別の鍵", PAGE_ID, build_token(SECRET, PAGE_ID))


def test_他人のページIDでは通らない() -> None:
    assert not verify_token(SECRET, "00000000-0000-0000-0000-000000000000", build_token(SECRET, PAGE_ID))


def test_署名が空なら通らない() -> None:
    assert not verify_token(SECRET, PAGE_ID, "")
    assert not verify_token("", PAGE_ID, "なんでも")


def test_鍵が無いまま発行しようとしたら例外() -> None:
    with pytest.raises(ValueError):
        build_token("", PAGE_ID)


def test_署名はURLに載せられる文字だけ() -> None:
    token = build_token(SECRET, PAGE_ID)
    assert "=" not in token and "+" not in token and "/" not in token


def test_URLの組み立て() -> None:
    url = build_unsubscribe_url("https://dash.example.com/", PAGE_ID, "TOKEN")
    assert url == "https://dash.example.com/unsubscribe?c=3ced8ea81234814a83cecb3645539acd&t=TOKEN"


def test_鍵は環境変数から読む(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BULK_EMAIL_UNSUBSCRIBE_SECRET", "  abc  ")
    assert load_secret() == "abc"
    monkeypatch.delenv("BULK_EMAIL_UNSUBSCRIBE_SECRET")
    assert load_secret() == ""
