"""「その相手に送ってよい根拠」を持つ層（純粋関数のみ、2026-09-03）。

■ なぜ要るか

これまで持っていたのは`ContactMailPreference`＝**送ってはいけない人の名簿**（配信停止）
だけだった。名簿に載っていない＝送ってよい、ではない。

```
   配信停止の名簿だけ                 根拠を持つ（この層）
   ─────────────────────────────      ─────────────────────────────
   載っていない ＝ 送る                根拠がある   ＝ 送る
                                      根拠が無い   ＝ 送らない（既定）
   ＝ 名刺交換もしていない相手にも     ＝ なぜ送ってよいかを1件ずつ持つ
     送れてしまう
```

特定電子メール法は、広告宣伝メールを**あらかじめ同意を得た相手**に送ることを原則に
している（名刺交換などでアドレスを通知された相手・取引関係にある相手・公表されている
事業用アドレスは例外として認められている）。Notionに連絡先が3,782件あることは
「送ってよい」の証明にはならない。

**根拠が確認できない相手は既定で送信不可**にする。これがこのモジュールの役割。

■ 根拠の種類（特定電子メール法 第3条第1項の各号に対応させている）

```
   opt_in       本人が同意した                    （1号）
   notified     本人からアドレスを受け取った        （2号・名刺交換など）
   transaction  取引関係にある                     （3号）
   published    ウェブサイト等で公表されている      （4号・法人／営業を営む個人）
```

★ 条文の当てはめは**法務の確認が要る**。ここでの分類は運用の目安であって、
法的な判断そのものではない（`docs/bulk_email_design_note.md`に記載）。

■ 根拠は「その連絡先の、そのアドレス」に紐づく

```
   送ってはいけない（配信停止）   ──▶ 広く効かせる。ページIDでもアドレスでも止める
   送ってよい（この層）          ──▶ 狭く効かせる。ページIDとアドレスの両方が一致した時だけ
```

名刺交換で得たのは**そのとき教えてもらったアドレス**であって、その人が将来使う
どのアドレスでもない。連絡先のアドレスが後から書き換わったら、根拠は付いてこない。
**登録し直してもらう。**（ChatGPTレビューがBLOCKERとして指摘、2026-09-03）

同じ理由で、**別の連絡先レコードの根拠をメールアドレス経由で借りることもしない。**
`info@` のような代表アドレスは会社をまたいで使い回されるうえ、元の連絡先がNotionから
消えても根拠の行はPostgresに残るため、「消えた人の根拠で別の人に送る」が成立してしまう。
同じ人が2レコードに登録されているなら、**2件とも登録する**（1件ずつ人が判断する、という
この機能の原則とも合う）。

■ 判定は「有効／無効」の2つだけ。古い根拠は警告に留める

```
   記録が無い        ──▶ 送らない
   取り消されている   ──▶ 送らない
   種類が不明        ──▶ 送らない（コードを変えたのに古い行が残っている等）
   証跡が空          ──▶ 送らない（後から誰も裏を取れないものは根拠ではない）
   取得日が無い      ──▶ 送らない（いつの根拠か言えないものは根拠ではない）
   取得日が未来      ──▶ 送らない（「明日 名刺交換した」は成立しない）
   アドレスが違う     ──▶ 送らない（根拠を登録したときのアドレスと今の宛先が別）
   取得から3年超     ──▶ 送るが警告を出す（★ここを「送らない」にするかは業務判断）
```

古さを送信不可にしないのは、名刺交換から3年経ったことと、取引関係が切れたことは
別の話だから。**機械が判断できるのは日付だけ**なので、日付だけで止めない。

■ 日付は「暦の日」で見る。UTCの時刻差で未来日にしない

取得日は日付であって時刻ではない。UTCの`datetime`同士で引き算すると、
**日本時間の午前中に「今日」を登録した瞬間に「1時間未来の日付」として送信不可**になる
（Geminiレビュー指摘、2026-09-03）。業務のタイムゾーン（JST）の暦日で比べる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from src.bulk_email.ids import normalize_page_id

# 業務上の「今日」を決めるタイムゾーン。取得日は暦の日として扱うため、
# UTCのまま引き算しない（日本時間の午前中に当日を登録すると未来日になってしまう）。
BUSINESS_TIMEZONE = timezone(timedelta(hours=9))

BASIS_OPT_IN = "opt_in"
BASIS_NOTIFIED = "notified"
BASIS_TRANSACTION = "transaction"
BASIS_PUBLISHED = "published"

# dictの順がそのまま画面の選択肢の並び。上ほど根拠として強い。
BASIS_LABELS: dict[str, str] = {
    BASIS_OPT_IN: "本人が同意した",
    BASIS_NOTIFIED: "本人からアドレスを受け取った（名刺交換・問い合わせ等）",
    BASIS_TRANSACTION: "取引関係にある",
    BASIS_PUBLISHED: "ウェブサイト等で公表されているアドレス",
}

# 画面に出す補足。「どれを選べばよいか」を選ぶ人がその場で判断できるようにする。
BASIS_DESCRIPTIONS: dict[str, str] = {
    BASIS_OPT_IN: "資料請求・セミナー申込・メルマガ登録などで、案内を受け取ることに同意した相手。",
    BASIS_NOTIFIED: "名刺交換・問い合わせ・メールでのやり取りで、本人からアドレスを教えてもらった相手。",
    BASIS_TRANSACTION: "契約・受注・導入支援など、実際の取引がある（あった）相手。",
    BASIS_PUBLISHED: "会社のサイトなどに事業用として公開されているアドレス。個人のアドレスは対象外。",
}

# 証跡の書き方の例（自由記述だが、空では登録させない）。
BASIS_EVIDENCE_HINTS: dict[str, str] = {
    BASIS_OPT_IN: "例）2026-05-12 の資料請求フォーム（Notion 案件ID 1234）",
    BASIS_NOTIFIED: "例）2026-04-08 大阪ホテル展の名刺交換。名刺は Drive の営業/名刺 に保管",
    BASIS_TRANSACTION: "例）リピッテホテル 2025-11 契約（Notion 案件ID 5678）",
    BASIS_PUBLISHED: "例）https://example.co.jp/contact に info@ を掲載（2026-09-01 確認）",
}

# 判定の理由コード。`audience.SKIP_REASON_LABELS`へそのまま合流する。
REASON_MISSING = "consent_missing"
REASON_REVOKED = "consent_revoked"
REASON_UNKNOWN_BASIS = "consent_unknown_basis"
REASON_NO_DATE = "consent_no_date"
REASON_FUTURE_DATE = "consent_future_date"
REASON_NO_EVIDENCE = "consent_no_evidence"
REASON_EMAIL_MISMATCH = "consent_email_mismatch"

REASON_LABELS: dict[str, str] = {
    REASON_MISSING: "送ってよい根拠が未登録",
    REASON_REVOKED: "送ってよい根拠が取り消し済み",
    REASON_UNKNOWN_BASIS: "根拠の種類が不明（登録し直しが必要）",
    REASON_NO_DATE: "根拠の取得日が未登録",
    REASON_FUTURE_DATE: "根拠の取得日が未来の日付",
    REASON_NO_EVIDENCE: "根拠の取得元・証跡が未登録",
    REASON_EMAIL_MISMATCH: "根拠を登録したときとメールアドレスが違う（登録し直しが必要）",
}

# 取得からこの年数を超えたら「古い」として警告する（送信は止めない）。
# ★画面の文言もここから作る。`dashboard`側に「3年」と書かない
#   （しきい値を変えたときに表示だけ嘘になるため。obasan-qualityレビュー指摘、2026-09-03）。
STALE_AFTER_YEARS = 3
STALE_AFTER_DAYS = 365 * STALE_AFTER_YEARS
STALE_LABEL = f"{STALE_AFTER_YEARS}年以上前"


@dataclass(frozen=True)
class ConsentRecord:
    """連絡先1件ぶんの「送ってよい根拠」。`ContactMailConsent`テーブルの1行に対応する。"""

    contact_page_id: str
    # 根拠を登録したときのメールアドレス（小文字）。**今の宛先と一致しなければ送らない。**
    contact_email: str = ""
    basis: str = ""
    # 根拠を得た日（暦の日）。**無い＝根拠として扱わない**（いつのものか言えない根拠は根拠ではない）。
    obtained_at: date | None = None
    # 取得元・証跡の自由記述。空では登録させない（登録画面側で必須にしている）。
    evidence: str = ""
    # 誤登録の訂正・取引終了などで根拠を取り消した日時。入っていれば送信不可。
    revoked_at: datetime | None = None
    recorded_by: str = ""

    @property
    def basis_label(self) -> str:
        return BASIS_LABELS.get(self.basis, self.basis or "（未設定）")


@dataclass(frozen=True)
class ConsentDecision:
    """1件ぶんの判定結果。"""

    allowed: bool
    # allowed=False のときの理由コード（`REASON_*`）。allowed=True では空。
    reason: str = ""
    detail: str = ""
    # 送れるが根拠が古い。件数を警告として出すために持つ。
    stale: bool = False

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


def _as_utc(value: datetime) -> datetime:
    """naiveなdatetimeはUTCとみなす（DBからはtimezone付きで来るが、テストの素の値も受ける）。"""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _as_date(value: date | datetime) -> date:
    """`datetime`が混ざっても暦の日として扱う。

    DBの列は`DATE`なので通常は`date`で来るが、古いデータや別経路が`datetime`を
    渡してくることがある。時刻を持ったまま比較すると、JSTの午前中に当日が
    「未来」になる不具合が戻ってくる。
    """
    if isinstance(value, datetime):
        return _as_utc(value).astimezone(BUSINESS_TIMEZONE).date()
    return value


def business_today(now: datetime | None = None) -> date:
    """業務上の「今日」（JSTの暦日）。"""
    return _as_utc(now or datetime.now(timezone.utc)).astimezone(BUSINESS_TIMEZONE).date()


def normalize_consent_email(value: str | None) -> str:
    """根拠の照合に使うアドレスの形。

    `audience.normalize_email`と同じ形にそろえる必要があるため、そちらを使う
    （`strip().lower()`を各所に直書きすると、将来どちらかだけ変わったときに
    照合が静かに壊れる。Geminiレビュー指摘、2026-09-03）。
    """
    from src.bulk_email.audience import normalize_email

    return normalize_email(value)


def evaluate(
    record: ConsentRecord | None,
    *,
    contact_email: str | None = None,
    now: datetime | None = None,
) -> ConsentDecision:
    """根拠1件を見て、送ってよいかを決める。

    **記録が無いときは送らない。** ここが「既定で送信不可」の実体で、
    この関数が`True`を返す条件を緩めることは、そのまま法令上のリスクになる。

    `contact_email`は今まさに送ろうとしている宛先。根拠を登録したときのアドレスと
    違えば送らない（名刺交換で得たのは「そのとき教えてもらったアドレス」であって、
    その人が将来使うどのアドレスでもないため）。
    """
    if record is None:
        return ConsentDecision(allowed=False, reason=REASON_MISSING)
    if record.revoked_at is not None:
        return ConsentDecision(
            allowed=False,
            reason=REASON_REVOKED,
            detail=_as_utc(record.revoked_at).astimezone(BUSINESS_TIMEZONE).date().isoformat(),
        )
    if record.basis not in BASIS_LABELS:
        return ConsentDecision(
            allowed=False, reason=REASON_UNKNOWN_BASIS, detail=record.basis or ""
        )
    if not (record.evidence or "").strip():
        # DBのCHECK制約でも空を弾いているが、判定はここが唯一の持ち主という前提を
        # 崩さないために置く（ChatGPTレビュー指摘、2026-09-03）。
        return ConsentDecision(allowed=False, reason=REASON_NO_EVIDENCE)
    if record.obtained_at is None:
        return ConsentDecision(allowed=False, reason=REASON_NO_DATE)

    recorded_email = normalize_consent_email(record.contact_email)
    current_email = normalize_consent_email(contact_email)
    if recorded_email != current_email:
        return ConsentDecision(
            allowed=False,
            reason=REASON_EMAIL_MISMATCH,
            detail=recorded_email or "（未登録）",
        )

    obtained = _as_date(record.obtained_at)
    today = business_today(now)
    if obtained > today:
        return ConsentDecision(
            allowed=False, reason=REASON_FUTURE_DATE, detail=obtained.isoformat()
        )
    return ConsentDecision(allowed=True, stale=(today - obtained).days > STALE_AFTER_DAYS)


class ConsentIndex:
    """連絡先ページIDで根拠を引く表。

    **メールアドレスでは引かない。** 同じアドレスの別の連絡先に登録された根拠を
    借りられるようにしていた時期があったが、次の2つの理由でやめた
    （ChatGPTがBLOCKERとして指摘、動物チームとGeminiも同じ経路を危険視、2026-09-03）。

    ```
       ① 代表アドレスは会社をまたいで使い回される
          取引先Aの sales@agency.jp と 取引先Bの sales@agency.jp は別人
       ② 元の連絡先がNotionから消えても、根拠の行はPostgresに残る
          同じアドレスの連絡先が後から作られると、消えた人の根拠で送れてしまう
    ```

    アドレスは「引くための鍵」ではなく「一致していることを確かめる対象」として使う
    （`evaluate()`が根拠のアドレスと今の宛先を突き合わせる）。
    """

    def __init__(self, records: Iterable[ConsentRecord] = ()) -> None:
        self._by_page_id: dict[str, ConsentRecord] = {}
        for record in records:
            page_id = normalize_page_id(record.contact_page_id)
            if page_id:
                self._by_page_id.setdefault(page_id, record)

    def find(self, contact_page_id: str) -> ConsentRecord | None:
        return self._by_page_id.get(normalize_page_id(contact_page_id))

    def decide(
        self, contact_page_id: str, email: str | None = None, *, now: datetime | None = None
    ) -> ConsentDecision:
        return evaluate(self.find(contact_page_id), contact_email=email, now=now)

    def __len__(self) -> int:
        return len(self._by_page_id)
