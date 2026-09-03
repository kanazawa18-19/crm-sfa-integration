"""連絡先ごとの返信ラグ・返信時間帯の検証（2026-09-03）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analytics.reply_timing import (
    EmailEvent,
    build_contact_insight,
    build_insights,
    classify_confidence,
    format_lag,
    pair_replies,
    reply_lag_stats,
    reply_timing_profile,
)

UTC = timezone.utc


def _event(
    direction: str,
    *,
    at: datetime,
    email: str = "a@example.com",
    thread: str | None = None,
) -> EmailEvent:
    return EmailEvent(contact_email=email, direction=direction, sent_at=at, thread_id=thread)


def _utc(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------- pair_replies


def test_pair_replies_empty_input_returns_empty_list() -> None:
    assert pair_replies([]) == []


def test_pair_replies_matches_inbound_to_preceding_outbound() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 10)),
            _event("inbound", at=_utc(9, 1, 13)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [3 * 3600]


def test_pair_replies_uses_latest_outbound_when_sent_repeatedly() -> None:
    """追撃で3通送った場合、起点は最後の1通（最初の1通から数えると実態より長く出る）。"""
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 9)),
            _event("outbound", at=_utc(9, 2, 9)),
            _event("outbound", at=_utc(9, 3, 9)),
            _event("inbound", at=_utc(9, 3, 11)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [2 * 3600]


def test_pair_replies_ignores_inbound_without_preceding_outbound() -> None:
    """相手からの新規メールが先に来ているケース。返信ではないので数えない。"""
    pairs = pair_replies(
        [
            _event("inbound", at=_utc(9, 1, 9)),
            _event("outbound", at=_utc(9, 1, 10)),
        ]
    )
    assert pairs == []


def test_pair_replies_consumes_outbound_so_later_inbound_is_not_double_counted() -> None:
    """1通の送信に返信が2通来ても、返信ペアは1件だけ。"""
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 10)),
            _event("inbound", at=_utc(9, 1, 11)),
            _event("inbound", at=_utc(9, 1, 12)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [3600]


def test_pair_replies_drops_inbound_beyond_max_lag() -> None:
    """15日後の受信は「返信」とみなさない（別件の新規メールの可能性が高い）。"""
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(8, 1, 10)),
            _event("inbound", at=_utc(8, 16, 10)),
        ]
    )
    assert pairs == []


def test_pair_replies_max_lag_boundary_is_inclusive() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(8, 1, 10)),
            _event("inbound", at=_utc(8, 15, 10)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [14 * 24 * 3600]


def test_pair_replies_separates_contacts() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 10), email="a@example.com"),
            _event("outbound", at=_utc(9, 1, 11), email="b@example.com"),
            _event("inbound", at=_utc(9, 1, 12), email="b@example.com"),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [3600]


def test_pair_replies_is_case_insensitive_on_contact_email() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 10), email="A@Example.com"),
            _event("inbound", at=_utc(9, 1, 11), email="a@example.com"),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [3600]


def test_pair_replies_treats_naive_datetime_as_utc() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=datetime(2026, 9, 1, 10)),
            _event("inbound", at=datetime(2026, 9, 1, 12)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [2 * 3600]


def test_pair_replies_orders_outbound_before_inbound_at_same_timestamp() -> None:
    pairs = pair_replies(
        [
            _event("inbound", at=_utc(9, 1, 10)),
            _event("outbound", at=_utc(9, 1, 10)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [0]


# ------------------------------------------------------------- reply_lag_stats


def test_reply_lag_stats_empty_input_reports_none_confidence() -> None:
    stats = reply_lag_stats([])
    assert stats.sample_size == 0
    assert stats.median_seconds is None
    assert stats.confidence == "none"


def test_reply_lag_stats_median_is_not_dragged_by_one_outlier() -> None:
    """中央値を主指標にする理由そのものの検証。1件の長期放置で平均だけが跳ねる。"""
    events = [
        _event("outbound", at=_utc(9, 1, 9)),
        _event("inbound", at=_utc(9, 1, 10)),  # 1時間
        _event("outbound", at=_utc(9, 2, 9)),
        _event("inbound", at=_utc(9, 2, 10)),  # 1時間
        _event("outbound", at=_utc(9, 3, 9)),
        _event("inbound", at=_utc(9, 13, 9)),  # 10日
    ]
    stats = reply_lag_stats(pair_replies(events))
    assert stats.sample_size == 3
    assert stats.median_seconds == 3600
    assert stats.mean_seconds is not None and stats.mean_seconds > 24 * 3600
    assert stats.fastest_seconds == 3600
    assert stats.slowest_seconds == 10 * 24 * 3600


def test_reply_lag_stats_median_of_even_count_averages_middle_two() -> None:
    events = []
    for i, hours in enumerate([1, 2, 3, 10]):
        base = _utc(9, 1 + i, 0)
        events.append(_event("outbound", at=base))
        events.append(_event("inbound", at=base + timedelta(hours=hours)))
    stats = reply_lag_stats(pair_replies(events))
    assert stats.median_seconds == int(2.5 * 3600)


# -------------------------------------------------------- reply_timing_profile


def test_reply_timing_profile_empty_input_reports_none_confidence() -> None:
    profile = reply_timing_profile([])
    assert profile.sample_size == 0
    assert profile.top_buckets == ()
    assert profile.confidence == "none"


def test_reply_timing_profile_counts_in_jst_not_utc() -> None:
    """UTC 23:00 は JST 翌日 08:00。JSTで数えていないと06-09時のコマに入らない。"""
    profile = reply_timing_profile([_event("inbound", at=_utc(9, 1, 23))])
    assert profile.buckets[2].label == "06-09時"
    assert profile.buckets[2].count == 1
    # UTCのままなら21-24時のコマに入ってしまう。
    assert profile.buckets[7].count == 0


def test_reply_timing_profile_weekday_is_in_jst() -> None:
    """2026-09-01(火) 23:00 UTC = 2026-09-02(水) 08:00 JST。"""
    profile = reply_timing_profile([_event("inbound", at=_utc(9, 1, 23))])
    assert profile.top_weekdays == ("水",)


def test_reply_timing_profile_ignores_outbound() -> None:
    profile = reply_timing_profile(
        [
            _event("outbound", at=_utc(9, 1, 1)),
            _event("outbound", at=_utc(9, 1, 2)),
            _event("inbound", at=_utc(9, 1, 3)),
        ]
    )
    assert profile.sample_size == 1


def test_reply_timing_profile_top_buckets_sorted_by_count_then_hour() -> None:
    events = [
        # JST 12時台に3件（UTC 03時）
        _event("inbound", at=_utc(9, 1, 3)),
        _event("inbound", at=_utc(9, 2, 3)),
        _event("inbound", at=_utc(9, 3, 3)),
        # JST 09時台に1件（UTC 00時）
        _event("inbound", at=_utc(9, 1, 0)),
        # JST 18時台に1件（UTC 09時）
        _event("inbound", at=_utc(9, 1, 9)),
    ]
    profile = reply_timing_profile(events)
    assert [b.label for b in profile.top_buckets] == ["12-15時", "09-12時", "18-21時"]
    assert profile.top_buckets[0].count == 3


def test_reply_timing_profile_excludes_empty_buckets_from_top() -> None:
    profile = reply_timing_profile([_event("inbound", at=_utc(9, 1, 3))])
    assert len(profile.top_buckets) == 1


def test_reply_timing_profile_rejects_bucket_size_not_dividing_24() -> None:
    try:
        reply_timing_profile([], bucket_size=5)
    except ValueError:
        return
    raise AssertionError("bucket_size=5 は24を割り切れないのでValueErrorになるべき")


# ---------------------------------------------------------------- 信頼度・整形


def test_classify_confidence_boundaries() -> None:
    assert classify_confidence(0) == "none"
    assert classify_confidence(1) == "low"
    assert classify_confidence(4) == "low"
    assert classify_confidence(5) == "medium"
    assert classify_confidence(9) == "medium"
    assert classify_confidence(10) == "high"


def test_format_lag_reads_as_japanese() -> None:
    assert format_lag(None) == "—"
    assert format_lag(0) == "0秒"
    assert format_lag(90) == "1分"
    assert format_lag(3600) == "1時間"
    assert format_lag(9000) == "2時間30分"
    assert format_lag(200000) == "2日7時間"
    assert format_lag(172800) == "2日"


# --------------------------------------------------------------- まとめて作る


def test_build_contact_insight_counts_both_directions() -> None:
    events = [
        _event("outbound", at=_utc(9, 1, 9)),
        _event("inbound", at=_utc(9, 1, 10)),
        _event("outbound", at=_utc(9, 2, 9)),
    ]
    insight = build_contact_insight("a@example.com", events)
    assert insight.outbound_count == 2
    assert insight.inbound_count == 1
    assert insight.lag.sample_size == 1
    assert insight.last_inbound_at == _utc(9, 1, 10)
    assert insight.last_outbound_at == _utc(9, 2, 9)


def test_build_insights_keys_are_lowercased_emails() -> None:
    insights = build_insights(
        [
            _event("outbound", at=_utc(9, 1, 9), email="A@Example.com"),
            _event("inbound", at=_utc(9, 1, 10), email="a@example.com"),
        ]
    )
    assert list(insights) == ["a@example.com"]
    assert insights["a@example.com"].lag.sample_size == 1


# --- 境界・異常系（QAレビュー指摘、2026-09-03） -----------------------------------------------


def test_median_of_even_count_truncates_the_half_second() -> None:
    """秒が奇数和になるケース。`(a+b)//2`なので切り捨てになる、を固定する。"""
    events = [
        _event("outbound", at=_utc(9, 1, 0)),
        _event("inbound", at=_utc(9, 1, 0, 0) + timedelta(seconds=10)),
        _event("outbound", at=_utc(9, 2, 0)),
        _event("inbound", at=_utc(9, 2, 0, 0) + timedelta(seconds=13)),
    ]
    stats = reply_lag_stats(pair_replies(events))
    # (10 + 13) // 2 = 11（11.5ではない）
    assert stats.median_seconds == 11


def test_reply_lag_stats_handles_odd_second_values() -> None:
    events = [
        _event("outbound", at=_utc(9, 1, 0)),
        _event("inbound", at=_utc(9, 1, 0) + timedelta(seconds=7)),
    ]
    stats = reply_lag_stats(pair_replies(events))
    assert stats.median_seconds == 7
    assert stats.mean_seconds == 7


def test_reply_timing_profile_rejects_zero_bucket_size() -> None:
    try:
        reply_timing_profile([], bucket_size=0)
    except ValueError:
        return
    raise AssertionError("bucket_size=0 はValueErrorになるべき")


def test_reply_timing_profile_rejects_negative_bucket_size() -> None:
    try:
        reply_timing_profile([], bucket_size=-3)
    except ValueError:
        return
    raise AssertionError("bucket_size=-3 はValueErrorになるべき")


def test_reply_timing_profile_accepts_hourly_buckets() -> None:
    profile = reply_timing_profile([_event("inbound", at=_utc(9, 1, 3))], bucket_size=1)
    assert len(profile.buckets) == 24
    assert profile.buckets[12].label == "12-13時"
    assert profile.buckets[12].count == 1


def test_format_lag_clamps_negative_seconds_to_zero() -> None:
    """相手のDateヘッダがこちらの送信より前にずれているケース（時計ずれ）。"""
    assert format_lag(-5) == "0秒"


def test_pair_replies_across_daylight_free_timezone_is_stable() -> None:
    """JSTには夏時間が無い。UTC→JSTの変換で日付が繰り上がるだけ、を明示しておく。"""
    profile = reply_timing_profile(
        [
            _event("inbound", at=_utc(9, 1, 14, 59)),  # JST 23:59（火）
            _event("inbound", at=_utc(9, 1, 15, 0)),  # JST 翌00:00（水）
        ]
    )
    assert profile.buckets[7].count == 1  # 21-24時
    assert profile.buckets[0].count == 1  # 00-03時
    assert profile.weekday_counts[1] == 1  # 火
    assert profile.weekday_counts[2] == 1  # 水


def test_build_insights_groups_blank_emails_together_without_crashing() -> None:
    """`contactEmail`が空のログ（NULL→""）が混ざっても落ちない。"""
    insights = build_insights(
        [
            _event("outbound", at=_utc(9, 1, 9), email=""),
            _event("inbound", at=_utc(9, 1, 10), email=""),
        ]
    )
    assert insights[""].lag.sample_size == 1


# --- スレッド単位の返信判定（ChatGPTレビュー指摘、2026-09-03） -------------------------------


def test_pair_replies_does_not_pair_across_threads() -> None:
    """月曜に送った見積書と、火曜に届いた別件のメールをペアにしない。

    スレッドを見ないと、本当の返信（金曜・4日）が消えて1日として記録される。
    """
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 9), thread="t-見積"),
            _event("inbound", at=_utc(9, 2, 9), thread="t-請求"),  # 別件
            _event("inbound", at=_utc(9, 5, 9), thread="t-見積"),  # 本当の返信
        ]
    )
    assert [p.lag_seconds for p in pairs] == [4 * 24 * 3600]


def test_pair_replies_tracks_each_thread_independently() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 9), thread="t1"),
            _event("outbound", at=_utc(9, 1, 10), thread="t2"),
            _event("inbound", at=_utc(9, 1, 11), thread="t2"),  # 1時間
            _event("inbound", at=_utc(9, 1, 12), thread="t1"),  # 3時間
        ]
    )
    assert sorted(p.lag_seconds for p in pairs) == [3600, 3 * 3600]


def test_pair_replies_falls_back_to_time_order_when_thread_is_unknown() -> None:
    """2026-09-03より前に記録した行はスレッドidを持たない。捨てずに従来どおり数える。"""
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 9)),
            _event("inbound", at=_utc(9, 1, 10)),
        ]
    )
    assert [p.lag_seconds for p in pairs] == [3600]


def test_pair_replies_keeps_unknown_thread_separate_from_known_threads() -> None:
    """スレッドidの有無が混ざっても、互いのペアを奪い合わない。"""
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 9), thread="t1"),
            _event("outbound", at=_utc(9, 1, 10)),  # スレッド不明
            _event("inbound", at=_utc(9, 1, 11), thread="t1"),  # t1への返信（2時間）
        ]
    )
    assert [p.lag_seconds for p in pairs] == [2 * 3600]


def test_pair_replies_ignores_inbound_on_a_thread_we_never_sent_to() -> None:
    pairs = pair_replies(
        [
            _event("outbound", at=_utc(9, 1, 9), thread="t1"),
            _event("inbound", at=_utc(9, 1, 10), thread="t2"),
        ]
    )
    assert pairs == []


def test_reply_timing_profile_reports_no_trend_when_the_distribution_is_flat() -> None:
    """24件が8つの時間帯に3件ずつ。件数は十分でも「傾向」は無い。"""
    events = []
    for bucket in range(8):
        for _ in range(3):
            # 各バケットの先頭時刻（JST）に3件ずつ置く
            jst_hour = bucket * 3
            utc_hour = (jst_hour - 9) % 24
            day = 1 if jst_hour >= 9 else 2
            events.append(_event("inbound", at=_utc(9, day, utc_hour)))

    profile = reply_timing_profile(events)

    assert profile.sample_size == 24
    assert profile.confidence == "high"
    assert profile.is_flat is True
    assert profile.top_buckets == ()


def test_reply_timing_profile_reports_a_trend_when_one_bucket_stands_out() -> None:
    events = [_event("inbound", at=_utc(9, 1, 3)) for _ in range(5)]
    events += [_event("inbound", at=_utc(9, 1, 0)) for _ in range(2)]

    profile = reply_timing_profile(events)

    assert profile.is_flat is False
    assert [b.label for b in profile.top_buckets][0] == "12-15時"


def test_reply_timing_profile_single_bucket_is_not_flat() -> None:
    profile = reply_timing_profile([_event("inbound", at=_utc(9, 1, 3))])
    assert profile.is_flat is False
    assert len(profile.top_buckets) == 1


def test_reply_timing_profile_ties_at_the_top_hide_the_weekday_ranking() -> None:
    """曜日も同じ扱い。同数なら月曜が先、という並びを「傾向」として見せない。"""
    profile = reply_timing_profile(
        [
            _event("inbound", at=_utc(9, 1, 3)),  # 火
            _event("inbound", at=_utc(9, 2, 3)),  # 水
        ]
    )
    assert profile.top_weekdays == ()
