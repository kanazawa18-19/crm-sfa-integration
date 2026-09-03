"""配信停止（`ContactMailPreference`）と送信根拠（`ContactMailConsent`）の読み取り（2026-09-03）。

スキーマ管理はdashboard側のPrisma（`dashboard/prisma/schema.prisma`）に一本化して
おり、ここではraw SQLで読むだけ（`src/gmail_sync/db.py`と同じ方針。同一DBに対する
二重のマイグレーション履歴を作らない）。

**書き込みはここにはない。** 配信停止の登録はお客様が開く公開ページ
（`dashboard/app/unsubscribe/`）から、送信根拠の登録は社内の管理画面
（`dashboard/app/(dashboard)/bulk-email/consent/`）から、どちらもPrisma経由で行う。
読む側と書く側が分かれているのは、書き込みが「人の操作」でしか起きないことを
コードの形で示すため。

```
   ContactMailPreference   送ってはいけない人の名簿   お客様が停止リンクから登録
   ContactMailConsent      送ってよい根拠            社内の担当者が登録
```
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from src.bulk_email.audience import normalize_email
from src.bulk_email.consent import ConsentRecord
from src.bulk_email.ids import normalize_page_id


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeout / timezone=UTC を明示する理由は`src/gmail_sync/db.py`と同じ。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def fetch_opt_outs(
    page_ids: Iterable[str], emails: Iterable[str]
) -> tuple[set[str], set[str]]:
    """候補の中から、配信停止の申し出がある連絡先ページIDとメールアドレスを返す。

    テーブル全件ではなく候補で絞るのは、宛先が数十件でもテーブルが数千行に育ちうるため
    （全件読みは行数の伸びに気づけないまま遅くなる）。

    **失敗しても空集合を返さない。** 例外はそのまま呼び出し元へ投げる。
    ここで握り潰すと「配信停止の人が0人」として扱われ、止めた相手に送ってしまう。
    """
    # DBの`contactPageId`は正規化済みの形（ハイフン無し・小文字）で保存されている
    # （公開ページがURLのcパラメータをそのまま入れるため）。一方Notionから来る
    # ページIDはハイフン付き。**照合は正規化した形で行い、返すのは呼び出し元が
    # 持っている元の形**にする（呼び出し元は元の形で除外判定をするため）。
    by_normalized: dict[str, str] = {}
    for page_id in page_ids:
        normalized = normalize_page_id(page_id)
        if normalized:
            by_normalized.setdefault(normalized, page_id)
    # 正規化は`audience.normalize_email`に一本化する。ここだけ`strip().lower()`を
    # 直書きすると、将来どちらかだけ変わったときに照合が静かに壊れる
    # （Geminiレビュー指摘、2026-09-03）。
    addresses = sorted({normalize_email(email) for email in emails} - {""})
    if not by_normalized and not addresses:
        return set(), set()

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "contactPageId", "contactEmail" FROM "ContactMailPreference" '
            'WHERE "unsubscribed" = TRUE '
            'AND ("contactPageId" = ANY(%s) OR lower("contactEmail") = ANY(%s))',
            (sorted(by_normalized), addresses),
        )
        rows = cur.fetchall()

    opted_out_ids = {
        by_normalized[normalize_page_id(row["contactPageId"])]
        for row in rows
        if normalize_page_id(row["contactPageId"] or "") in by_normalized
    }
    opted_out_emails = {normalize_email(row["contactEmail"]) for row in rows}
    opted_out_emails.discard("")
    return opted_out_ids, opted_out_emails


def fetch_consents(page_ids: Iterable[str]) -> list[ConsentRecord]:
    """候補の連絡先に登録済みの「送ってよい根拠」を返す。

    **ページIDでしか引かない。** メールアドレスでも引くと、同じアドレスの
    別の連絡先（別会社の代表アドレス、Notionから消えた連絡先の残骸）の根拠まで
    拾ってしまう。配信停止（`fetch_opt_outs`）がアドレスでも引くのとは逆で、
    **「送るな」は広く、「送ってよい」は狭く**効かせる。

    **取り消し済み（`revokedAt`が入っている）行も含めて返す。** 判定側で
    「未登録」と「取り消し済み」を言い分けるため（画面では直し方が違う）。

    `fetch_opt_outs`と同じく、**失敗しても空リストを返さない。** 例外はそのまま投げる。
    ここで握り潰すと「根拠が1件も無い」＝全員送信不可、として静かに宛先が0件になり、
    設定漏れなのかDB障害なのかが画面から区別できなくなる。
    """
    normalized_ids = sorted({normalize_page_id(page_id) for page_id in page_ids} - {""})
    if not normalized_ids:
        return []

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "contactPageId", "contactEmail", "basis", "obtainedAt", "evidence", '
            '"revokedAt", "recordedBy" FROM "ContactMailConsent" '
            'WHERE "contactPageId" = ANY(%s)',
            (normalized_ids,),
        )
        rows = cur.fetchall()

    return [
        ConsentRecord(
            contact_page_id=row["contactPageId"] or "",
            contact_email=normalize_email(row["contactEmail"]),
            basis=row["basis"] or "",
            obtained_at=row["obtainedAt"],
            evidence=row["evidence"] or "",
            revoked_at=row["revokedAt"],
            recorded_by=row["recordedBy"] or "",
        )
        for row in rows
    ]
