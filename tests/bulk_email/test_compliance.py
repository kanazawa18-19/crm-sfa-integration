"""特定電子メール法の表示（送信者・配信停止）の検証（2026-09-03）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.bulk_email.compliance import (
    FOOTER_MARKER,
    SenderIdentity,
    append_footer,
    build_footer,
    contains_footer,
    load_sender_identity,
    missing_sender_fields,
)

FILLED = SenderIdentity(
    company_name="テスト株式会社",
    postal_code="100-0001",
    address="東京都千代田区1-1-1",
    contact_email="info@example.com",
    contact_url="https://example.com/",
)


def test_空欄を日本語ラベルで列挙する() -> None:
    assert missing_sender_fields(SenderIdentity()) == [
        "会社名",
        "郵便番号",
        "住所",
        "問い合わせ先メールアドレス",
        "問い合わせ先URL",
    ]


def test_全部埋まっていれば空欄なし() -> None:
    assert missing_sender_fields(FILLED) == []


def test_フッターに会社情報と配信停止URLが載る() -> None:
    footer = build_footer(FILLED, "https://dash.example.com/unsubscribe?c=x&t=y")
    assert "テスト株式会社" in footer
    assert "〒100-0001 東京都千代田区1-1-1" in footer
    assert "https://dash.example.com/unsubscribe?c=x&t=y" in footer
    assert FOOTER_MARKER in footer


def test_配信停止URLが無いときは空リンクを載せず理由を書く() -> None:
    """それらしい本文に見せてしまうと、止められないメールが送れる状態と区別が付かない。"""
    footer = build_footer(FILLED, "")
    assert "このメールは送れません" in footer


def test_本文の末尾にフッターを付ける() -> None:
    assert append_footer("本文です\n\n", "FOOTER") == "本文です\nFOOTER"


def test_テンプレートに法定表示が既に含まれていたら検出する() -> None:
    """過去のメールを貼り付けると、そこに残る配信停止リンクは別の宛先のもの。"""
    assert contains_footer(f"本文\n{FOOTER_MARKER}\n…")
    assert not contains_footer("本文")


def test_設定ファイルを読む(tmp_path: Path) -> None:
    path = tmp_path / "sender.json"
    path.write_text(json.dumps({"company_name": "テスト株式会社"}), encoding="utf-8")
    assert load_sender_identity(path).company_name == "テスト株式会社"


def test_環境変数が設定ファイルより優先される(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sender.json"
    path.write_text(json.dumps({"company_name": "ファイル側"}), encoding="utf-8")
    monkeypatch.setenv("BULK_EMAIL_SENDER_COMPANY_NAME", "環境変数側")
    assert load_sender_identity(path).company_name == "環境変数側"


def test_設定ファイルが無くても例外にせず空で返す(tmp_path: Path) -> None:
    """例外にすると、設定漏れが「画面が真っ白」として出てしまい原因が分からない。"""
    identity = load_sender_identity(tmp_path / "ない.json")
    assert identity.company_name == ""
    assert missing_sender_fields(identity)


def test_リポジトリの設定ファイルは実在してJSONとして読める() -> None:
    """初期状態では全項目が空。**推測で埋めない**（誤った住所を数千通に載せない）。"""
    path = Path(__file__).resolve().parents[2] / "config" / "bulk_email_sender.json"
    assert json.loads(path.read_text(encoding="utf-8"))["_comment"]
