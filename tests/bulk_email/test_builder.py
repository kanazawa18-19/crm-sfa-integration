"""1通1通の組み立て（`build_messages`）の検証（2026-09-03）。

この関数がこのプロジェクトで一番壊してはいけない判断（誰に送るか・法定表示が
揃っているか）を持つ。DBもNotionも起動せずに検証できることが設計の要件そのもの
なので、このテストにはフェイクもモックも出てこない。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.bulk_email.audience import (
    SKIP_MISSING_MERGE_VALUE,
    SKIP_UNSUBSCRIBED,
    Contact,
)
from src.bulk_email.compliance import SenderIdentity
from src.bulk_email.builder import build_messages
from src.bulk_email.consent import (
    BASIS_NOTIFIED,
    REASON_MISSING,
    STALE_AFTER_DAYS,
    ConsentRecord,
)

NOW = datetime(2026, 9, 3, 6, tzinfo=timezone.utc)  # JST 9/3 15:00

IDENTITY = SenderIdentity(
    company_name="テスト株式会社",
    postal_code="100-0001",
    address="東京都千代田区1-1-1",
    contact_email="info@example.com",
    contact_url="https://example.com/",
)
SECRET = "test-secret"
BASE_URL = "https://dash.example.com"


def contact(page_id: str = "p1", **kwargs: object) -> Contact:
    values: dict = {
        "page_id": page_id,
        "name": "山田",
        "email": f"{page_id}@example.com",
        "department": "営業部",
        "title": "部長",
        "client_name": "テスト商事",
    }
    values.update(kwargs)
    return Contact(**values)  # type: ignore[arg-type]


def allow(*page_ids: str, days_ago: int = 30, **kwargs: object) -> list[ConsentRecord]:
    """根拠は「登録時のアドレス＝今の宛先」でしか効かないので、
    `contact()`が作る`<page_id>@example.com`に合わせる。"""
    values: dict = {
        "basis": BASIS_NOTIFIED,
        "obtained_at": NOW.date() - timedelta(days=days_ago),
        "evidence": "2026-08 展示会で名刺交換",
    }
    values.update(kwargs)
    return [
        ConsentRecord(
            contact_page_id=page_id, contact_email=f"{page_id}@example.com", **values
        )
        for page_id in page_ids
    ]


def build(**kwargs: object):
    params: dict = {
        "subject": "{{会社名}}様へのご案内",
        "body": "{{会社名}}\n{{氏名}} 様\n\nいつもお世話になっております。",
        "contacts": [contact()],
        "sender_name": "金沢",
        "identity": IDENTITY,
        "unsubscribe_secret": SECRET,
        "unsubscribe_base_url": BASE_URL,
        "now": NOW,
    }
    params.update(kwargs)
    # 既定では「渡した宛先には全員ぶんの根拠が登録済み」にする。
    # 根拠そのものを見るテストだけが`consents`を明示的に渡す。
    if "consents" not in params:
        params["consents"] = allow(*[c.page_id for c in params["contacts"]])
    return build_messages(**params)  # type: ignore[arg-type]


def test_差し込み後の件名と本文が返る() -> None:
    result = build()
    assert result.sendable
    assert result.blockers == ()
    message = result.messages[0]
    assert message.subject == "テスト商事様へのご案内"
    assert message.body.startswith("テスト商事\n山田 様")
    assert message.to_email == "p1@example.com"


def test_本文の末尾に法定表示と配信停止リンクが必ず付く() -> None:
    body = build().messages[0].body
    assert "テスト株式会社" in body
    assert "〒100-0001 東京都千代田区1-1-1" in body
    assert f"{BASE_URL}/unsubscribe?c=p1&t=" in body


def test_宛先ごとに配信停止リンクが違う() -> None:
    result = build(contacts=[contact("p1"), contact("p2")])
    links = {message.body.split("/unsubscribe?")[1].split("\n")[0] for message in result.messages}
    assert len(links) == 2


def test_送信者情報が空なら送れない() -> None:
    result = build(identity=SenderIdentity())
    assert not result.sendable
    assert any("送信者情報が未設定" in blocker for blocker in result.blockers)
    # 文面の確認自体はできること（本文は組み立てて返す）。
    assert result.messages


def test_署名鍵や配信停止ページのURLが無ければ送れない() -> None:
    assert any("署名鍵" in b for b in build(unsubscribe_secret="").blockers)
    assert any("配信停止ページのURL" in b for b in build(unsubscribe_base_url="").blockers)


def test_鍵が無いときはそれらしいリンクを作らない() -> None:
    body = build(unsubscribe_secret="").messages[0].body
    assert "/unsubscribe?" not in body
    assert "このメールは送れません" in body


def test_差し込み名の綴り間違いは送れない() -> None:
    result = build(body="{{御社名}} 様")
    assert any("知らない差し込み名" in blocker for blocker in result.blockers)


def test_件名と本文が空なら送れない() -> None:
    result = build(subject="  ", body="")
    assert any("件名が空" in b for b in result.blockers)
    assert any("本文が空" in b for b in result.blockers)


def test_過去のメールを貼り付けた本文は送れない() -> None:
    """そこに残っている配信停止リンクは別の宛先のもの。"""
    pasted = build().messages[0].body
    result = build(body=pasted)
    assert any("法定表示" in blocker for blocker in result.blockers)


def test_差し込む値が空の宛先は落とす() -> None:
    """「〇〇様」のつもりが「　様」になったメールを送らない。"""
    result = build(contacts=[contact("p1", name=""), contact("p2")])
    assert [m.contact_page_id for m in result.messages] == ["p2"]
    assert [(s.contact.page_id, s.reason) for s in result.skipped] == [
        ("p1", SKIP_MISSING_MERGE_VALUE)
    ]


def test_差出人名が未設定なら担当者名の差し込みは落ちる() -> None:
    result = build(body="{{担当者名}} より", sender_name="")
    assert result.messages == ()
    assert result.skipped[0].reason == SKIP_MISSING_MERGE_VALUE


def test_配信停止の申し出がある宛先は外れる() -> None:
    result = build(contacts=[contact("p1"), contact("p2")], opted_out_page_ids=["p1"])
    assert [m.contact_page_id for m in result.messages] == ["p2"]
    assert result.skipped[0].reason == SKIP_UNSUBSCRIBED


def test_宛先が1件も無ければ送れない() -> None:
    result = build(contacts=[])
    assert not result.sendable
    assert any("宛先が1件もありません" in blocker for blocker in result.blockers)


def test_連絡先の読み込みが打ち切られたら警告する() -> None:
    """送ったつもりで送っていない相手がいる、という形で表に出るため黙って捨てない。"""
    result = build(truncated_client_names=["テスト商事"])
    assert any("先頭までしか読み込めていません" in w for w in result.warnings)
    # 警告であって、送れないわけではない。
    assert result.sendable


def test_使われている差し込み名を返す() -> None:
    assert build().placeholders_used == ("会社名", "氏名")


def test_件名に改行があれば送れない() -> None:
    """件名はヘッダー1行。②で実送信するときのヘッダーインジェクション対策を入口で止める。"""
    result = build(subject="件名\nBcc: someone@example.com")
    assert any("件名に改行" in blocker for blocker in result.blockers)


# ── 送ってよい根拠（オプトイン）との接続 ───────────────────────────────


def test_根拠を渡さなければ1通も組み立てない() -> None:
    """引数の渡し忘れは「うっかり全員に送る」ではなく「1通も送れない」側に倒す。"""
    result = build(consents=[])
    assert result.messages == ()
    assert not result.sendable
    assert [s.reason for s in result.skipped] == [REASON_MISSING]


def test_根拠が無くて0件のときは直し方まで出す() -> None:
    """「0件です」だけでは何をすればよいか分からない。"""
    result = build(consents=[])
    assert any("送ってよい根拠" in b and "送信根拠の管理" in b for b in result.blockers)


def test_根拠が古い宛先は警告に出るが送れる() -> None:
    result = build(consents=allow("p1", days_ago=STALE_AFTER_DAYS + 1))
    assert result.sendable
    assert any("3年以上たっている" in w for w in result.warnings)
