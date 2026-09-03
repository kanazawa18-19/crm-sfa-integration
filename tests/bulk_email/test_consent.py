"""「送ってよい根拠」の判定（`src/bulk_email/consent.py`）の検証（2026-09-03）。

ここが緩むと、名刺交換もしていない相手に営業メールを送る形になる。
**判定が`allowed=True`を返す条件を、テストで固定しておく。**
"""

from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timedelta, timezone

from src.bulk_email.consent import (
    BASIS_DESCRIPTIONS,
    BASIS_EVIDENCE_HINTS,
    BASIS_LABELS,
    BASIS_NOTIFIED,
    BASIS_OPT_IN,
    REASON_EMAIL_MISMATCH,
    REASON_FUTURE_DATE,
    REASON_LABELS,
    REASON_MISSING,
    REASON_NO_EVIDENCE,
    REASON_NO_DATE,
    REASON_REVOKED,
    REASON_UNKNOWN_BASIS,
    STALE_AFTER_DAYS,
    STALE_AFTER_YEARS,
    STALE_LABEL,
    ConsentIndex,
    ConsentRecord,
    evaluate,
)

NOW = datetime(2026, 9, 3, 6, tzinfo=timezone.utc)  # JST 9/3 15:00
TODAY = date(2026, 9, 3)
EMAIL = "yamada@example.com"


def record(**kwargs: object) -> ConsentRecord:
    values: dict = {
        "contact_page_id": "3ced8ea81234814a83cecb3645539acd",
        "contact_email": "yamada@example.com",
        "basis": BASIS_NOTIFIED,
        "obtained_at": TODAY - timedelta(days=30),
        "evidence": "2026-08 展示会で名刺交換",
    }
    values.update(kwargs)
    return ConsentRecord(**values)  # type: ignore[arg-type]


def decide(rec: ConsentRecord | None, email: str = EMAIL) -> object:
    return evaluate(rec, contact_email=email, now=NOW)


def test_記録が無ければ送れない() -> None:
    """既定で送信不可。この1件がこのモジュールの存在理由。"""
    decision = decide(None)
    assert decision.allowed is False
    assert decision.reason == REASON_MISSING


def test_有効な根拠なら送れる() -> None:
    decision = decide(record())
    assert decision.allowed is True
    assert decision.reason == ""
    assert decision.stale is False


def test_取り消し済みは送れない() -> None:
    decision = decide(record(revoked_at=NOW - timedelta(days=1)))
    assert decision.allowed is False
    assert decision.reason == REASON_REVOKED
    # いつ取り消したかを画面に出せること。
    assert decision.detail == "2026-09-02"


def test_知らない種類は送れない() -> None:
    decision = decide(record(basis="むかしの種類"))
    assert decision.allowed is False
    assert decision.reason == REASON_UNKNOWN_BASIS
    assert decision.detail == "むかしの種類"


def test_取得日が無ければ送れない() -> None:
    assert decide(record(obtained_at=None)).reason == REASON_NO_DATE


def test_古い根拠は送れるが警告になる() -> None:
    old = decide(record(obtained_at=NOW - timedelta(days=STALE_AFTER_DAYS + 1)))
    assert old.allowed is True
    assert old.stale is True
    ちょうど = decide(record(obtained_at=NOW - timedelta(days=STALE_AFTER_DAYS)))
    assert ちょうど.stale is False


def test_取得日は暦の日として扱う() -> None:
    """DBのDATE列から来る`date`をそのまま比べる。"""
    assert decide(record(obtained_at=date(2026, 8, 4))).allowed is True


def test_ページIDは表記ゆれを吸収して引ける() -> None:
    index = ConsentIndex([record(contact_page_id="3ced8ea81234814a83cecb3645539acd")])
    assert index.decide("3CED8EA8-1234-814A-83CE-CB3645539ACD", EMAIL, now=NOW).allowed is True


def test_アドレスでは引かない() -> None:
    """代表アドレスの使い回しや、消えた連絡先の残骸で送れてしまうのを防ぐ。

    同じ人が2つの連絡先レコードに登録されているなら、2件とも登録してもらう。
    """
    index = ConsentIndex([record(contact_page_id="a" * 32)])
    assert index.decide("b" * 32, EMAIL, now=NOW).reason == REASON_MISSING


def test_登録時と宛先のアドレスが違えば送らない() -> None:
    """名刺交換で得たのは「そのとき教えてもらったアドレス」であって、
    その人が将来使うどのアドレスでもない。"""
    decision = decide(record(contact_email="old@example.com"), email="new@example.com")
    assert decision.allowed is False
    assert decision.reason == REASON_EMAIL_MISMATCH
    assert decision.detail == "old@example.com"


def test_アドレスの表記ゆれでは弾かない() -> None:
    assert decide(record(contact_email="  YAMADA@Example.COM ")).allowed is True


def test_証跡が空なら送らない() -> None:
    assert decide(record(evidence="   ")).reason == REASON_NO_EVIDENCE


def test_選択肢の定義が3つとも揃っている() -> None:
    """画面がラベル・説明・証跡の例を全部出せること（片方だけ足すと空欄になる）。"""
    assert set(BASIS_DESCRIPTIONS) == set(BASIS_LABELS)
    assert set(BASIS_EVIDENCE_HINTS) == set(BASIS_LABELS)
    assert BASIS_OPT_IN in BASIS_LABELS


def test_理由コードには日本語のラベルが付く() -> None:
    assert REASON_LABELS[REASON_MISSING] == "送ってよい根拠が未登録"


def test_未来の取得日では送れない() -> None:
    """「明日 名刺交換した」根拠は成立しない。画面側と同じ判断をここでも持つ。"""
    decision = decide(record(obtained_at=NOW + timedelta(days=1)))
    assert decision.allowed is False
    assert decision.reason == REASON_FUTURE_DATE
    assert decision.detail == "2026-09-04"


def test_当日の取得日は受け付ける() -> None:
    assert decide(record(obtained_at=NOW)).allowed is True


def test_古さのしきい値と表示文言が同じ値から作られる() -> None:
    """画面に「3年以上前」と直接書くと、しきい値を変えたときに文言だけ嘘になる。"""
    assert STALE_AFTER_DAYS == 365 * STALE_AFTER_YEARS
    assert STALE_LABEL == f"{STALE_AFTER_YEARS}年以上前"


def test_ダッシュボード側の種類の一覧と一致している() -> None:
    """PythonとTypeScriptの二重定義が食い違うと、画面から登録できるのに
    バックエンドが「種類が不明」として送信不可にする（気づきにくい壊れ方）。
    コメントで「両方直すこと」と書くだけでは守れないので、値を突き合わせる。"""
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "dashboard"
        / "lib"
        / "bulkEmailConsent.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"CONSENT_BASES = \[(.*?)\] as const", source, re.S)
    assert match, "CONSENT_BASES の定義が見つかりません（定義の書き方を変えたら、ここも直す）"
    ts_bases = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert ts_bases == set(BASIS_LABELS), (
        f"Python側={sorted(BASIS_LABELS)} / TypeScript側={sorted(ts_bases)}。"
        "src/bulk_email/consent.py と dashboard/lib/bulkEmailConsent.ts の両方を直してください。"
    )
