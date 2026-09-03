"""連絡先ごとの「返信の返ってきやすさ」を数える（2026-09-03）。

`EmailLog`（`src/gmail_sync/`がGmailから取り込む送受信ログ）だけを入力に、
2つの指標を出す。

```
   ① 返信ラグ        こちらが送ってから、相手が返してくるまでの時間
   ② 返信時間帯      相手からのメールが実際に届いている曜日・時刻の偏り
```

■ 「返信」とみなす条件（仕様書に定義が無いためここで決めた）

```
   送信 ──▶ 送信 ──▶ 送信 ──▶ 受信          直前の送信を起点にする
                     └───ラグ───┘            （連続送信は「追撃」であり、
                                               返信を引き出したのは最後の1通）
```

- **起点は直前のoutbound**。同じ相手へ連続して送っている場合は最後の1通を使う。
  最初の1通から数えると、追撃を繰り返すほどラグが実態より長く出てしまう。
- **`_MAX_REPLY_LAG_DAYS`日を超えたinboundは返信とみなさない**。1か月後に届いた
  メールは前の送信への返信ではなく、別件の新規メールである可能性の方が高いため。
  除外した場合、そのinboundは「次の送信を待つ」のではなく単に捨てる
  （返信ペアからは外れるが、②の時間帯ヒストグラムには引き続き数える）。
- **②はinbound全件**を数える（返信ペアに絞らない）。「相手が席にいてメールを
  触っている時間帯」を知りたいのであって、返信かどうかは問わないため。
  返信ペアだけに絞るとサンプル数が一段減り、後述の信頼度がほぼ`low`になる。

■ 平均ではなく中央値を主指標にする

金沢さんの要望は「平均返信ラグ」だが、返信ラグは**右に長く裾を引く**分布になる
（大半は数時間、稀に1週間）。平均は数件の長期放置に引っ張られて実態より大きく出る。
`mean_seconds`も返すが、画面で主に見せるのは`median_seconds`とする。

■ サンプル数が足りないときは、足りないと言う

`EmailLog`は2026-08-25のGmail連携開始以降しか無く、2026-09-03時点で実測308件
（inboundは31件のみ）。連絡先1件あたりの返信は数件しかない。**件数を書かずに
「この人は14時に返信しやすい」と表示すると、たまたま1件がそうだっただけの偶然を
断定として読ませてしまう。** そのため全ての結果に`sample_size`と`confidence`を
必ず付ける。呼び出し側は`confidence == "none"`を「データなし」として扱うこと。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Sequence
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# これを超えて届いたinboundは、直前の送信への「返信」とみなさない（上記参照）。
_MAX_REPLY_LAG_DAYS = 14

# 時間帯ヒストグラムのバケット幅（時間）。1時間刻みだと現在のサンプル数では
# ほぼ全てのバケットが0か1になり、山が見えないため3時間でまとめる。
_HOUR_BUCKET_SIZE = 3

# 信頼度の境界。件数がこれ未満なら1段下がる。
#
# **統計的な裏付けがある数字ではなく、営業が読むときの目安として置いた値。**
# 「1〜4件は、たまたまその時間に返ってきただけかもしれない」「10件あれば曜日・時間帯の
# 山を1つは信じてよい」という感覚に合わせている。根拠のある値に差し替えたくなったら、
# 実データが貯まった後に`scripts/verify_gmail_backfill.py`の分布を見て決め直すこと。
_CONFIDENCE_HIGH = 10
_CONFIDENCE_MEDIUM = 5

Confidence = Literal["high", "medium", "low", "none"]

WEEKDAY_LABELS_JA = ("月", "火", "水", "木", "金", "土", "日")


@dataclass(frozen=True)
class EmailEvent:
    """`EmailLog`1行のうち、この分析に必要な最小項目。

    `sent_at`はtz-awareでもnaiveでもよい。naiveの場合はUTCとして解釈する
    （Postgres側が`TIMESTAMP(3)`のtz無し列にUTC値を入れている前提。
    `src/gmail_sync/db.py`の`_connect()`が接続時にtimezone=UTCへ固定している）。
    """

    contact_email: str
    direction: str  # "inbound" | "outbound"
    sent_at: datetime


@dataclass(frozen=True)
class ReplyPair:
    """返信1件（起点の送信と、それに対する受信）。"""

    outbound_at: datetime
    inbound_at: datetime

    @property
    def lag_seconds(self) -> int:
        return int((self.inbound_at - self.outbound_at).total_seconds())


@dataclass(frozen=True)
class ReplyLagStats:
    """返信ラグの集計結果。件数0のときは各統計値がNoneになる。"""

    sample_size: int
    median_seconds: int | None
    mean_seconds: int | None
    fastest_seconds: int | None
    slowest_seconds: int | None
    confidence: Confidence


@dataclass(frozen=True)
class HourBucket:
    """時間帯1コマ分（`start_hour`以上`end_hour`未満、JST）。"""

    start_hour: int
    end_hour: int
    count: int

    @property
    def label(self) -> str:
        return f"{self.start_hour:02d}-{self.end_hour:02d}時"


@dataclass(frozen=True)
class ReplyTimingProfile:
    """時間帯・曜日の偏り。`top_buckets`は件数の多い順（同数なら早い時間帯が先）。"""

    sample_size: int
    buckets: tuple[HourBucket, ...]
    weekday_counts: tuple[int, ...]  # 月=0 〜 日=6
    top_buckets: tuple[HourBucket, ...]
    top_weekdays: tuple[str, ...]
    confidence: Confidence


@dataclass(frozen=True)
class ContactReplyInsight:
    """連絡先1件分のまとめ（①と②の両方）。

    `key`はグルーピングに使った識別子（メールアドレスのことも、Notionの連絡先ページIDの
    こともある。`build_contact_insight()`のdocstring参照）。
    """

    key: str
    lag: ReplyLagStats
    timing: ReplyTimingProfile
    outbound_count: int
    inbound_count: int
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def classify_confidence(sample_size: int) -> Confidence:
    """件数から信頼度を決める。0件は`"none"`（「データが無い」と「傾向が弱い」を区別する）。"""
    if sample_size <= 0:
        return "none"
    if sample_size >= _CONFIDENCE_HIGH:
        return "high"
    if sample_size >= _CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def pair_replies(
    events: Iterable[EmailEvent], *, max_lag_days: int = _MAX_REPLY_LAG_DAYS
) -> list[ReplyPair]:
    """同一連絡先の送受信を時系列に並べ、送信→受信の返信ペアを作る。

    複数の連絡先が混ざったリストを渡してもよい（内部で連絡先ごとに分けて処理する）。
    返り値は受信時刻の昇順。
    """
    by_contact: dict[str, list[EmailEvent]] = {}
    for event in events:
        by_contact.setdefault(event.contact_email.lower(), []).append(event)

    limit = timedelta(days=max_lag_days)
    pairs: list[ReplyPair] = []
    for contact_events in by_contact.values():
        # 同時刻に送信と受信が並んだ場合は送信を先に見る（送信の直後に返信が来た、
        # という並びの方が自然で、かつ後段の「直前の送信」判定が安定するため）。
        ordered = sorted(
            contact_events,
            key=lambda e: (_as_utc(e.sent_at), 0 if e.direction == "outbound" else 1),
        )
        pending_outbound_at: datetime | None = None
        for event in ordered:
            at = _as_utc(event.sent_at)
            if event.direction == "outbound":
                # 連続送信は上書きする＝常に「直前の送信」が起点になる。
                pending_outbound_at = at
                continue
            if pending_outbound_at is None:
                continue
            if at - pending_outbound_at <= limit:
                pairs.append(ReplyPair(outbound_at=pending_outbound_at, inbound_at=at))
            # 返信として採用したかどうかに関わらず起点は消費する。残したままにすると、
            # 1通の送信に対して後続の受信が何通も返信として二重計上されてしまう。
            pending_outbound_at = None
    pairs.sort(key=lambda p: p.inbound_at)
    return pairs


def _median(values: Sequence[int]) -> int:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def reply_lag_stats(pairs: Sequence[ReplyPair]) -> ReplyLagStats:
    """返信ペアから中央値・平均・最速・最遅を出す。"""
    if not pairs:
        return ReplyLagStats(
            sample_size=0,
            median_seconds=None,
            mean_seconds=None,
            fastest_seconds=None,
            slowest_seconds=None,
            confidence="none",
        )
    lags = [p.lag_seconds for p in pairs]
    return ReplyLagStats(
        sample_size=len(lags),
        median_seconds=_median(lags),
        mean_seconds=round(sum(lags) / len(lags)),
        fastest_seconds=min(lags),
        slowest_seconds=max(lags),
        confidence=classify_confidence(len(lags)),
    )


def reply_timing_profile(
    events: Iterable[EmailEvent], *, top_n: int = 3, bucket_size: int = _HOUR_BUCKET_SIZE
) -> ReplyTimingProfile:
    """inboundの受信時刻（JST）から、曜日・時間帯の偏りを出す。

    outboundは無視する（こちらの送信時刻は相手の都合を何も表さない）。
    """
    if bucket_size <= 0 or 24 % bucket_size != 0:
        raise ValueError(f"bucket_size must divide 24 evenly, got {bucket_size}")

    bucket_count = 24 // bucket_size
    counts = [0] * bucket_count
    weekday_counts = [0] * 7
    total = 0
    for event in events:
        if event.direction != "inbound":
            continue
        local = _as_utc(event.sent_at).astimezone(JST)
        counts[local.hour // bucket_size] += 1
        weekday_counts[local.weekday()] += 1
        total += 1

    buckets = tuple(
        HourBucket(start_hour=i * bucket_size, end_hour=(i + 1) * bucket_size, count=c)
        for i, c in enumerate(counts)
    )
    # 件数の多い順。同数なら早い時間帯を先に（並びが実行ごとに揺れないようにする）。
    top_buckets = tuple(
        b for b in sorted(buckets, key=lambda b: (-b.count, b.start_hour))[:top_n] if b.count > 0
    )
    top_weekdays = tuple(
        WEEKDAY_LABELS_JA[i]
        for i in sorted(range(7), key=lambda i: (-weekday_counts[i], i))[:top_n]
        if weekday_counts[i] > 0
    )
    return ReplyTimingProfile(
        sample_size=total,
        buckets=buckets,
        weekday_counts=tuple(weekday_counts),
        top_buckets=top_buckets,
        top_weekdays=top_weekdays,
        confidence=classify_confidence(total),
    )


def build_contact_insight(key: str, events: Sequence[EmailEvent]) -> ContactReplyInsight:
    """連絡先1件分の`EmailLog`行から①②をまとめて作る。

    `key`は**グルーピングに使った識別子をそのまま返すためだけ**の値で、この関数は中身を
    解釈しない。`build_insights()`はメールアドレスを渡すが、`src/api/reply_timing_service.py`と
    `scripts/verify_gmail_backfill.py`は**Notionの連絡先ページID**を渡す（同じ人が複数の
    アドレスを使っていても1人として数えたいため）。
    """
    inbound = [e for e in events if e.direction == "inbound"]
    outbound = [e for e in events if e.direction == "outbound"]
    return ContactReplyInsight(
        key=key,
        lag=reply_lag_stats(pair_replies(events)),
        timing=reply_timing_profile(events),
        outbound_count=len(outbound),
        inbound_count=len(inbound),
        last_inbound_at=max((_as_utc(e.sent_at) for e in inbound), default=None),
        last_outbound_at=max((_as_utc(e.sent_at) for e in outbound), default=None),
    )


def build_insights(events: Iterable[EmailEvent]) -> dict[str, ContactReplyInsight]:
    """連絡先メールアドレス（小文字）をキーに、連絡先ごとの分析結果を返す。"""
    by_contact: dict[str, list[EmailEvent]] = {}
    for event in events:
        by_contact.setdefault(event.contact_email.lower(), []).append(event)
    return {email: build_contact_insight(email, rows) for email, rows in by_contact.items()}


def format_lag(seconds: int | None) -> str:
    """秒数を「約2時間30分」のような日本語にする（画面・Slack通知の両方から使う）。

    非エンジニアが読む前提なので、秒やISO 8601ではなく素の日本語で返す。
    """
    if seconds is None:
        return "—"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分"
    hours, rem_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}時間{rem_minutes}分" if rem_minutes else f"{hours}時間"
    days, rem_hours = divmod(hours, 24)
    return f"{days}日{rem_hours}時間" if rem_hours else f"{days}日"
