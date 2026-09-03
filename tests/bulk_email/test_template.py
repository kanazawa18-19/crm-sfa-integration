"""本文テンプレートの差し込みの検証（2026-09-03）。"""

from __future__ import annotations

from src.bulk_email.template import (
    PLACEHOLDERS,
    find_placeholders,
    render,
    unknown_placeholders,
)


def test_差し込み名を出現順に重複なく拾う() -> None:
    text = "{{会社名}} {{氏名}} 様（{{会社名}}）"
    assert find_placeholders(text) == ["会社名", "氏名"]


def test_前後の空白を許す() -> None:
    assert find_placeholders("{{ 会社名 }}") == ["会社名"]


def test_改行をまたぐ波括弧は差し込みとみなさない() -> None:
    """本文中に単独で出てくる`{{`が、遠く離れた`}}`と誤って対にならないこと。"""
    assert find_placeholders("{{会社名\n氏名}}") == []


def test_知らない差し込み名を検出する() -> None:
    assert unknown_placeholders("{{会社名}}と{{御社名}}") == ["御社名"]


def test_値を差し込む() -> None:
    result = render("{{会社名}}の{{氏名}}様", {"会社名": "テスト商事", "氏名": "山田"})
    assert result.text == "テスト商事の山田様"
    assert result.missing == ()


def test_値が空なら空欄にしてmissingへ載せる() -> None:
    result = render("{{会社名}}の{{部署}}", {"会社名": "テスト商事", "部署": "   "})
    assert result.text == "テスト商事の"
    assert result.missing == ("部署",)


def test_差し込んだ値の中の波括弧は再展開されない() -> None:
    """取引先名に`{{氏名}}`が紛れ込んでいても、そこが他人の値に置き換わらないこと。

    素朴にreplaceを繰り返す実装だと、ここで「山田」が出てしまう。
    """
    result = render("{{会社名}}", {"会社名": "{{氏名}}商事", "氏名": "山田"})
    assert result.text == "{{氏名}}商事"


def test_知らない差し込み名はそのまま残す() -> None:
    """空欄にして見えなくすると、綴り間違いに気づけなくなるため。"""
    result = render("{{御社名}}", {})
    assert result.text == "{{御社名}}"


def test_使える差し込み名には説明が付いている() -> None:
    assert all(description for description in PLACEHOLDERS.values())
