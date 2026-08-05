"""メンバー別パフォーマンス評価（ボリューム×クオリティ×スピード）の検証。"""

from __future__ import annotations

from datetime import date

from src.analytics.member_performance import (
    MemberActionRecord,
    MemberProjectRecord,
    compute_member_performance,
    member_contact_counts,
    member_deadline_compliance_rates,
    member_win_rates,
)

AS_OF = date(2026, 8, 7)


# --- member_contact_counts（ボリューム） ---


def test_member_contact_counts_aggregates_countable_action_types_only() -> None:
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=AS_OF),
        MemberActionRecord(project_id="P1", member="佐藤", action_type="訪問商談", action_date=AS_OF),
        MemberActionRecord(project_id="P2", member="佐藤", action_type="メール", action_date=AS_OF),  # 人力メールは対象外
        MemberActionRecord(project_id="P3", member="鈴木", action_type="オンライン商談", action_date=AS_OF),
    ]

    counts = member_contact_counts(actions)

    assert counts == {"佐藤": 2, "鈴木": 1}


def test_member_contact_counts_returns_empty_dict_for_no_actions() -> None:
    assert member_contact_counts([]) == {}


# --- member_win_rates（クオリティ） ---


def test_member_win_rates_returns_none_when_member_has_no_decided_projects() -> None:
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="提案中"),
        MemberProjectRecord(project_id="P2", member="佐藤", status="商談中(B)"),
    ]

    rates = member_win_rates(projects)

    assert rates == {"佐藤": None}


def test_member_win_rates_computed_against_decided_projects_only() -> None:
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="契約済"),
        MemberProjectRecord(project_id="P2", member="佐藤", status="失注"),
        MemberProjectRecord(project_id="P3", member="佐藤", status="解約"),
        MemberProjectRecord(project_id="P4", member="佐藤", status="提案中"),  # 分母に含めない
    ]

    rates = member_win_rates(projects)

    assert rates == {"佐藤": 1 / 3}


def test_member_win_rates_is_zero_when_no_wins_among_decided_projects() -> None:
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="失注"),
        MemberProjectRecord(project_id="P2", member="佐藤", status="解約"),
    ]

    rates = member_win_rates(projects)

    assert rates == {"佐藤": 0.0}


# --- member_deadline_compliance_rates（スピード簡易代替指標） ---


def test_deadline_compliance_rate_is_none_when_no_projects_are_past_due() -> None:
    """次回アクション日が全て未来（または未設定）のケース。"""
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 10)
        ),
        MemberProjectRecord(project_id="P2", member="佐藤", status="提案中", next_action_date=None),
    ]

    rates = member_deadline_compliance_rates(projects, [], as_of=AS_OF)

    assert rates == {"佐藤": None}


def test_deadline_compliance_rate_counts_project_as_compliant_when_followed_up() -> None:
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
    ]

    rates = member_deadline_compliance_rates(projects, actions, as_of=AS_OF)

    assert rates == {"佐藤": 1.0}


def test_deadline_compliance_rate_counts_project_as_overdue_when_no_follow_up() -> None:
    """actionsは空ではない（アクションデータ自体は連携されている）が、期限超過案件に
    対する追いかけアクションが1件も無い、という「本当に遵守できなかった」ケース。"""
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        # P1とは別案件のアクション。アクションデータ自体は連携されていることを示す。
        MemberActionRecord(project_id="P2", member="佐藤", action_type="テレアポ", action_date=AS_OF),
    ]

    rates = member_deadline_compliance_rates(projects, actions, as_of=AS_OF)

    assert rates == {"佐藤": 0.0}


def test_deadline_compliance_rate_is_none_when_due_but_no_action_data_at_all() -> None:
    """次回アクション日が過去の案件はあるが、actionsが完全に空（アクションデータ未連携の
    可能性がある）ケース。「本当に遵守できなかった0%」と区別するため未確定(None)を返す。
    """
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]

    rates = member_deadline_compliance_rates(projects, [], as_of=AS_OF)

    assert rates == {"佐藤": None}


def test_deadline_compliance_rate_excludes_decided_projects() -> None:
    """決着済み(契約済/失注/解約)案件は、次回アクション日が過去でも期限判定対象から除外する。
    決着後は新しいアクションが発生しないため、含めると「スピード良く決着させた案件ほど
    次回アクション日フィールドが未消化のまま残り遵守率を下げる」という逆転が起きる。
    """
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="契約済", next_action_date=date(2026, 8, 1)
        ),
        MemberProjectRecord(
            project_id="P2", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        MemberActionRecord(
            project_id="P2", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)
        ),
    ]

    rates = member_deadline_compliance_rates(projects, actions, as_of=AS_OF)

    # P1(契約済)は分母から除外され、P2(提案中・フォロー済)のみが対象 -> 1.0
    # (除外されなければP1が期限超過に計上され0.5になるはず)
    assert rates == {"佐藤": 1.0}


def test_deadline_compliance_rate_counts_email_action_as_valid_follow_up() -> None:
    """followed_up判定はaction_typeを一切フィルタしない（人力メールも期限内フォローの
    証拠として認める）。ボリューム集計(COUNTABLE_ACTION_TYPESで人力メールを除外)とは
    非対称であることの境界テスト。
    """
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="メール", action_date=date(2026, 8, 2)),
    ]

    rates = member_deadline_compliance_rates(projects, actions, as_of=AS_OF)

    assert rates == {"佐藤": 1.0}


def test_deadline_compliance_rate_counts_action_on_exact_same_day_as_followed_up() -> None:
    """action_dateがnext_action_dateと厳密に同日の境界値。`>=`により遵守扱いとする仕様。"""
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 1)),
    ]

    rates = member_deadline_compliance_rates(projects, actions, as_of=AS_OF)

    assert rates == {"佐藤": 1.0}


def test_deadline_compliance_rate_ignores_action_before_next_action_date() -> None:
    """次回アクション日より前のアクションはフォロー実施とみなさない。"""
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 5)
        ),
    ]
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 3)),
    ]

    rates = member_deadline_compliance_rates(projects, actions, as_of=AS_OF)

    assert rates == {"佐藤": 0.0}


def test_deadline_compliance_rate_excludes_project_due_today() -> None:
    """次回アクション日がas_ofと同日（＝まだ過ぎていない）は判定対象外。"""
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="提案中", next_action_date=AS_OF),
    ]

    rates = member_deadline_compliance_rates(projects, [], as_of=AS_OF)

    assert rates == {"佐藤": None}


# --- compute_member_performance（総合スコア） ---


def test_compute_member_performance_returns_none_overall_score_for_member_with_no_projects() -> None:
    """担当案件0件のメンバー（アクションのみ存在）は総合スコアが未確定。"""
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=AS_OF),
    ]

    results = compute_member_performance([], actions, as_of=AS_OF)

    assert len(results) == 1
    assert results[0].member == "佐藤"
    assert results[0].quality_win_rate is None
    assert results[0].speed_compliance_rate is None
    assert results[0].overall_score is None


def test_compute_member_performance_returns_empty_tuple_for_no_data() -> None:
    assert compute_member_performance([], [], as_of=AS_OF) == ()


def test_compute_member_performance_volume_score_is_zero_when_no_contacts_at_all() -> None:
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="契約済"),
    ]

    results = compute_member_performance(projects, [], as_of=AS_OF)

    assert results[0].volume_contact_count == 0
    assert results[0].volume_score == 0.0


def test_compute_member_performance_normalizes_volume_relative_to_group_max() -> None:
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="契約済"),
        MemberProjectRecord(project_id="P2", member="鈴木", status="契約済"),
    ]
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=AS_OF)
        for _ in range(10)
    ] + [
        MemberActionRecord(project_id="P2", member="鈴木", action_type="テレアポ", action_date=AS_OF)
        for _ in range(5)
    ]

    results = compute_member_performance(projects, actions, as_of=AS_OF)
    by_member = {r.member: r for r in results}

    assert by_member["佐藤"].volume_score == 1.0
    assert by_member["鈴木"].volume_score == 0.5


def test_compute_member_performance_overall_score_is_product_of_three_metrics() -> None:
    """3指標が全て異なる非自明な値になるケースで、掛け算結果そのものを直接検証する。
    （1×1×1のような全て1.0のケースだと、min()等どんな集約方式でも同じ値になり、
    実際に「掛け算」であることを判別できないため、あえて非自明な値を用いる。）
    """
    projects = [
        # クオリティ用: 決着済み5件中3件受注 -> quality = 3/5 = 0.6
        MemberProjectRecord(project_id="Q1", member="佐藤", status="契約済"),
        MemberProjectRecord(project_id="Q2", member="佐藤", status="契約済"),
        MemberProjectRecord(project_id="Q3", member="佐藤", status="契約済"),
        MemberProjectRecord(project_id="Q4", member="佐藤", status="失注"),
        MemberProjectRecord(project_id="Q5", member="佐藤", status="解約"),
        # スピード用: 次回アクション日が過去の5件中4件フォロー済み -> speed = 1 - 1/5 = 0.8
        MemberProjectRecord(project_id="S1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)),
        MemberProjectRecord(project_id="S2", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)),
        MemberProjectRecord(project_id="S3", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)),
        MemberProjectRecord(project_id="S4", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)),
        MemberProjectRecord(project_id="S5", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)),
    ]
    actions = [
        MemberActionRecord(project_id="S1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
        MemberActionRecord(project_id="S2", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
        MemberActionRecord(project_id="S3", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
        MemberActionRecord(project_id="S4", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
        # S5には期限後のフォローなし -> 期限超過1件
        MemberActionRecord(project_id="Q1", member="佐藤", action_type="テレアポ", action_date=AS_OF),
    ] + [
        # ボリューム用: 鈴木は佐藤(5件)の2倍(10件)の接触回数 -> 佐藤のvolume_score = 0.5
        MemberActionRecord(project_id="S99", member="鈴木", action_type="テレアポ", action_date=AS_OF)
        for _ in range(10)
    ]

    results = compute_member_performance(projects, actions, as_of=AS_OF)
    by_member = {r.member: r for r in results}
    perf = by_member["佐藤"]

    assert perf.volume_contact_count == 5  # S1-S4(4件) + Q1(1件)
    assert perf.volume_score == 0.5
    assert perf.quality_win_rate == 0.6
    assert perf.speed_compliance_rate == 0.8
    assert perf.overall_score == 0.5 * 0.6 * 0.8
    assert perf.overall_score == 0.24


def test_compute_member_performance_overall_score_is_none_when_only_quality_is_none() -> None:
    """クオリティのみ未確定（決着済み案件0件）、スピードは確定しているケース。"""
    projects = [
        MemberProjectRecord(
            project_id="P1", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
    ]

    results = compute_member_performance(projects, actions, as_of=AS_OF)

    assert len(results) == 1
    perf = results[0]
    assert perf.quality_win_rate is None
    assert perf.speed_compliance_rate == 1.0
    assert perf.overall_score is None


def test_compute_member_performance_overall_score_is_none_when_only_speed_is_none() -> None:
    """スピードのみ未確定（次回アクション期限判定対象の案件が無い）、クオリティは確定
    しているケース。"""
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="契約済"),
    ]

    results = compute_member_performance(projects, [], as_of=AS_OF)

    assert len(results) == 1
    perf = results[0]
    assert perf.quality_win_rate == 1.0
    assert perf.speed_compliance_rate is None
    assert perf.overall_score is None


def test_compute_member_performance_overall_score_is_zero_when_volume_score_is_zero() -> None:
    """volume_score=0.0（接触実績なし）だがquality/speedが確定しているケースで、
    overall_scoreがNoneではなく正しく0.0になることを検証する。"""
    projects = [
        MemberProjectRecord(project_id="P1", member="佐藤", status="契約済"),
        MemberProjectRecord(
            project_id="P2", member="佐藤", status="提案中", next_action_date=date(2026, 8, 1)
        ),
    ]
    actions = [
        # 期限フォローはメールのみ（非COUNTABLE）= ボリュームには計上されないが、
        # followed_up判定には使われる（アクション種別を問わない仕様）。
        MemberActionRecord(project_id="P2", member="佐藤", action_type="メール", action_date=date(2026, 8, 2)),
    ]

    results = compute_member_performance(projects, actions, as_of=AS_OF)

    assert len(results) == 1
    perf = results[0]
    assert perf.volume_contact_count == 0
    assert perf.volume_score == 0.0
    assert perf.quality_win_rate == 1.0
    assert perf.speed_compliance_rate == 1.0
    assert perf.overall_score == 0.0
