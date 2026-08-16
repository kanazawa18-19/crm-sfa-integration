"""キーワード+重み付けスコアリングによるインシデント・アクシデント検知(2026-08-16)。

対象テキストは`subject + "\\n" + snippet`(`EmailLog`に既にあるフィールド)。カテゴリ
(`src.incident_detection.keywords.CATEGORIES`)ごとに、いずれかのキーワード/正規表現/
複合ペアが1つでもヒットすればそのカテゴリの重みを加算する(同一カテゴリ内で複数
ヒットしても加算は1回のみ、カテゴリ単位)。合計スコアで優先度を判定する:
- 8点以上 → "high"(即Slack通知)
- 4〜7点 → "medium"(日次ダイジェストでまとめて通知)
- 1〜3点 → "low"(通知せず記録のみ)
- 0点 → None(インシデントではない、通常のメール)
"""

from __future__ import annotations

import re

from src.incident_detection.keywords import CATEGORIES

_HIGH_THRESHOLD = 8
_MEDIUM_THRESHOLD = 4
_LOW_THRESHOLD = 1


def _combine_text(subject: str | None, snippet: str | None) -> str:
    return f"{subject or ''}\n{snippet or ''}"


def _matches_any_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _matches_any_regex(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _matches_any_compound_pair(text: str, pairs: list[tuple[str, str]]) -> bool:
    return any(all(word in text for word in pair) for pair in pairs)


def _category_hit(text: str, category: dict) -> bool:
    if _matches_any_keyword(text, category.get("keywords", [])):
        return True
    if _matches_any_regex(text, category.get("regex_keywords", [])):
        return True
    if _matches_any_compound_pair(text, category.get("compound_pairs", [])):
        return True
    return False


def _priority_for_score(score: int) -> str | None:
    if score >= _HIGH_THRESHOLD:
        return "high"
    if score >= _MEDIUM_THRESHOLD:
        return "medium"
    if score >= _LOW_THRESHOLD:
        return "low"
    return None


def score_email(subject: str | None, snippet: str | None) -> tuple[int, str | None]:
    """`subject`/`snippet`からインシデントスコアと優先度を判定する。

    スコアリング対象カテゴリがどれもヒットしなければ`(0, None)`を返す。
    """
    text = _combine_text(subject, snippet)
    score = sum(category["weight"] for category in CATEGORIES.values() if _category_hit(text, category))
    return score, _priority_for_score(score)
