"""チェックイン機の導入有無をWEB検索で確かめる(2026-09-11)。

楽天トラベルの施設ページには、チェックイン機のことはほとんど書かれていない
（2026-09-11、鳥取県40施設で実測したところ**40件すべて記載なし**だった）。
そのため施設ページからの一次判定だけでは事実上使いものにならず、
本人指示（2026-09-11）により「施設ページ＋WEB検索」の2段構えにしている。

    一次判定  施設ページの記述     無料・全件   `services.detect_check_in_machine()`
    二次判定  WEB検索で確認         有料・候補のみ  このモジュール

**見つからなければ`NO`ではなく`UNKNOWN`。** 「ネットに書かれていない」ことは
「導入していない」ことの証明にならない。`NO`を立てるのは、公式サイト等に
「フロントで対面チェックイン」と明記されている等、**否定の根拠が取れたときだけ**。

軽い定型判定なので `claude-haiku-4-5` を使う（AGENTS.md「作業の重さに合わせる」）。
`ANTHROPIC_API_KEY` が無ければ検索せず「未実行」を返すだけで、例外は投げない。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from src.facility_list.domain.models import (
    CheckInMachineSource,
    CheckInMachineStatus,
    Facility,
)

logger = logging.getLogger(__name__)

# 軽い事実確認なのでHaiku。日付サフィックスは付けない(AGENTS.md)。
MODEL_ID = "claude-haiku-4-5"

# Haiku 4.5 が使えるのは基本版のweb_search。`_20260209`(動的フィルタ付き)は
# Opus 5/4.8/4.7/4.6・Sonnet 5/4.6 専用なので、ここで使うと400になる。
_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 4,
}

_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": (
                "自動チェックイン機・セルフチェックイン端末の導入有無。"
                "導入している根拠が見つかれば yes。"
                "対面のみと明記されている等、導入していない根拠が見つかれば no。"
                "どちらの根拠も見つからなければ unknown。"
                "『書かれていない』は no ではなく unknown。"
            ),
        },
        "evidence": {
            "type": "string",
            "description": "そう判断した根拠を日本語1〜2文で。根拠が無ければ空文字。",
        },
        "evidence_url": {
            "type": "string",
            "description": "根拠にしたページのURL。無ければ空文字。",
        },
    },
    "required": ["status", "evidence", "evidence_url"],
    "additionalProperties": False,
}

_PROMPT = """次の宿泊施設が「自動チェックイン機（セルフチェックイン端末）」を導入しているか、\
ウェブ検索で調べてください。

施設名: {name}
所在地: {address}
楽天トラベル: {url}

判断の材料になるもの:
- 施設の公式サイトの「チェックイン方法」「ご利用案内」
- 自動チェックイン機メーカー（アルメックス、オムロン、NEC、富士通、NCR等）の導入事例
- ニュースリリース、宿泊者のクチコミ

**大事な決まり**
- 導入している根拠が見つかったときだけ yes。
- 「フロントで対面チェックイン」等、導入していない根拠が明確なときだけ no。
- 情報が見つからない場合は必ず unknown。推測で yes/no にしないこと。
- 同名の別施設の情報を使わないこと。所在地が一致するか確かめること。"""


@dataclass(frozen=True)
class CheckInMachineSearchResult:
    status: CheckInMachineStatus
    source: CheckInMachineSource
    evidence: str | None = None
    evidence_url: str | None = None
    # 検索そのものが行えなかった場合の理由(APIキー無し・API障害)。
    # 「調べた上で分からなかった」と「そもそも調べていない」を区別するために持つ。
    not_searched_reason: str | None = None

    @property
    def searched(self) -> bool:
        return self.not_searched_reason is None


def _response_text(message) -> str:  # noqa: ANN001
    """テキストブロックを走査して連結する。

    `content[0].text` を決め打ちしない（AGENTS.md）。thinkingやサーバーツールの
    結果ブロックが先頭に来ると壊れるため。
    """
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


def search_check_in_machine(facility: Facility) -> CheckInMachineSearchResult:
    """1施設について、WEB検索でチェックイン機の導入を確かめる。

    失敗しても例外を投げない（1件の失敗で一括処理を止めないため）。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return CheckInMachineSearchResult(
            status=CheckInMachineStatus.UNKNOWN,
            source=CheckInMachineSource.NONE,
            not_searched_reason="ANTHROPIC_API_KEYが設定されていない",
        )

    try:
        import anthropic
    except ImportError:
        return CheckInMachineSearchResult(
            status=CheckInMachineStatus.UNKNOWN,
            source=CheckInMachineSource.NONE,
            not_searched_reason="anthropicパッケージが入っていない",
        )

    client = anthropic.Anthropic()
    prompt = _PROMPT.format(
        name=facility.name,
        address=facility.address or f"{facility.prefecture or ''}{facility.city or ''}",
        url=facility.page_url,
    )

    try:
        message = client.messages.create(
            model=MODEL_ID,
            max_tokens=2000,
            tools=[_WEB_SEARCH_TOOL],
            output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - 1件の失敗で一括処理を止めない
        logger.warning("チェックイン機の検索に失敗した hotelNo=%s: %s", facility.hotel_no, exc)
        return CheckInMachineSearchResult(
            status=CheckInMachineStatus.UNKNOWN,
            source=CheckInMachineSource.NONE,
            not_searched_reason=f"APIエラー: {exc}",
        )

    # 応答が途中で切れていたらJSONとして読めない。stop_reasonを必ず見る。
    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning("応答が長さ上限で切れた hotelNo=%s", facility.hotel_no)
        return CheckInMachineSearchResult(
            status=CheckInMachineStatus.UNKNOWN,
            source=CheckInMachineSource.NONE,
            not_searched_reason="応答が長さ上限で切れた",
        )

    text = _response_text(message)
    try:
        payload = json.loads(text)
        status = CheckInMachineStatus(payload["status"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("応答を読み取れなかった hotelNo=%s: %s", facility.hotel_no, exc)
        return CheckInMachineSearchResult(
            status=CheckInMachineStatus.UNKNOWN,
            source=CheckInMachineSource.NONE,
            not_searched_reason=f"応答を読み取れなかった: {exc}",
        )

    evidence = (payload.get("evidence") or "").strip() or None
    evidence_url = (payload.get("evidence_url") or "").strip() or None

    # 根拠のないyes/noは採らない。営業が「なぜこの施設なのか」を確かめられないため。
    if status is not CheckInMachineStatus.UNKNOWN and not evidence:
        logger.info("根拠が無いので不明に倒した hotelNo=%s", facility.hotel_no)
        status = CheckInMachineStatus.UNKNOWN

    return CheckInMachineSearchResult(
        status=status,
        source=(
            CheckInMachineSource.WEB_SEARCH
            if status is not CheckInMachineStatus.UNKNOWN
            else CheckInMachineSource.NONE
        ),
        evidence=evidence,
        evidence_url=evidence_url,
    )
