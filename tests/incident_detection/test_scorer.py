"""キーワード+重み付けスコアリング(`src.incident_detection.scorer`)の単体テスト。"""

from __future__ import annotations

from src.incident_detection import scorer


def test_score_email_no_hit_returns_zero_and_none_priority() -> None:
    score, priority = scorer.score_email("来週の打ち合わせについて", "ご確認よろしくお願いいたします。")
    assert score == 0
    assert priority is None


def test_score_email_report_procedure_category_hits_weight_5() -> None:
    score, priority = scorer.score_email(None, "経緯報告書をご確認ください")
    assert score == 5
    assert priority == "medium"


def test_score_email_contract_legal_risk_category_hits_weight_5() -> None:
    score, priority = scorer.score_email("契約違反について", None)
    assert score == 5
    assert priority == "medium"


def test_score_email_data_incident_category_hits_weight_5() -> None:
    score, priority = scorer.score_email(None, "データ削除が発生しました")
    assert score == 5
    assert priority == "medium"


def test_score_email_incident_trouble_definition_category_hits_weight_3() -> None:
    score, priority = scorer.score_email(None, "不具合が発生しています")
    assert score == 3
    assert priority == "low"


def test_score_email_billing_category_hits_weight_3() -> None:
    score, priority = scorer.score_email(None, "請求ミスがあったので返金をお願いします")
    assert score == 3
    assert priority == "low"


def test_score_email_service_outage_category_hits_weight_3() -> None:
    score, priority = scorer.score_email(None, "機能が停止中のようです")
    assert score == 3
    assert priority == "low"


def test_score_email_apology_category_hits_weight_2() -> None:
    score, priority = scorer.score_email(None, "大変申し訳ございません")
    assert score == 2
    assert priority == "low"


def test_score_email_customer_emotion_category_hits_weight_2() -> None:
    score, priority = scorer.score_email(None, "大変残念に思います")
    assert score == 2
    assert priority == "low"


def test_score_email_time_delay_neglect_keyword_hits_weight_2() -> None:
    score, priority = scorer.score_email(None, "この件、放置されていた状態です")
    assert score == 2
    assert priority == "low"


def test_score_email_time_delay_neglect_regex_pattern_months() -> None:
    score, priority = scorer.score_email(None, "1ヶ月以上お待たせしてしまいました")
    assert score == 2
    assert priority == "low"


def test_score_email_time_delay_neglect_regex_pattern_days() -> None:
    score, priority = scorer.score_email(None, "対応まで10日間かかっています")
    assert score == 2
    assert priority == "low"


def test_score_email_compound_pair_hits_only_when_both_words_present() -> None:
    score, priority = scorer.score_email(None, "設定に漏れがありました")
    assert score == 2
    assert priority == "low"


def test_score_email_compound_pair_does_not_hit_with_only_one_word() -> None:
    # 「設定」のみで「漏れ」「不備」「違い」のいずれも無ければ複合キーワードとしてはヒットしない
    score, priority = scorer.score_email(None, "設定を変更しました")
    assert score == 0
    assert priority is None


def test_score_email_multiple_keywords_in_same_category_add_weight_only_once() -> None:
    # 「不具合」「エラー」「トラブル」はいずれも「事故トラブル定義」カテゴリ(weight=3)。
    # 同一カテゴリ内で複数ヒットしても加算は1回のみ。
    score, priority = scorer.score_email(None, "不具合とエラーとトラブルが同時に発生しました")
    assert score == 3
    assert priority == "low"


def test_score_email_multiple_categories_sum_weights() -> None:
    # 「経緯報告書」(報告手続き, weight=5) + 「申し訳ございません」(謝罪陳謝, weight=2) = 7
    score, priority = scorer.score_email("経緯報告書のご送付", "申し訳ございません、深くお詫び申し上げます")
    assert score == 7
    assert priority == "medium"


def test_score_email_boundary_score_exactly_4_is_medium() -> None:
    # 事故トラブル定義(weight=3) + 謝罪陳謝(weight=2)では7になってしまうため、
    # weight=3のカテゴリ1つ+weight=2のカテゴリの複合キーワードでは境界値4にならない。
    # ここでは謝罪陳謝(2) + 顧客感情クレーム(2) = 4 を境界値ちょうどのケースとして使う。
    score, priority = scorer.score_email(None, "申し訳ございません、大変遺憾に思います")
    assert score == 4
    assert priority == "medium"


def test_score_email_boundary_score_exactly_7_is_medium() -> None:
    # 報告手続き(5) + 謝罪陳謝(2) = 7
    score, priority = scorer.score_email(None, "経緯報告書を送ります。申し訳ございません。")
    assert score == 7
    assert priority == "medium"


def test_score_email_boundary_score_exactly_8_is_high() -> None:
    # 報告手続き(5) + 事故トラブル定義(3) = 8
    score, priority = scorer.score_email(None, "経緯報告書を送ります。不具合が発生しました。")
    assert score == 8
    assert priority == "high"


def test_score_email_boundary_score_at_low_range_minimum_achievable_value() -> None:
    # このカテゴリ定義では最小weightが2(weight=1のカテゴリは存在しない)ため、
    # 実際に発生しうる1〜3点帯の最小値は2点となる。それでも"low"判定になることを検証する。
    score, priority = scorer.score_email(None, "多大なご迷惑をおかけしました")
    assert score == 2
    assert priority == "low"
