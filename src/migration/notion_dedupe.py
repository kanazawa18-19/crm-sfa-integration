"""既存Notionデータとの名寄せ（重複防止）ロジック。

ZohoデータをNotionへ書き込む前に、既にNotion側に存在するページと同一のレコードを
重複作成してしまわないよう、既存ページのスナップショットを取得し、正規化した会社名で
突合する。突合できた場合でも、郵便番号が大きく食い違っていれば「要確認」として
自動では確定させない（誤結合防止、2026-08-10金沢さん確認済みの方針:
「自動で書き込まない」設計により、一致しなかった・確信が持てないケースはレポートに
出力するだけに留め、実際の書き込みは人の目でレビューしてから確定させる）。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.migration.zoho_client_master import (
    normalize_company_name_basic,
    normalize_company_name_strong,
)
from src.sync_engine.clients.notion_client import HttpNotionClient, parse_notion_property_value


@dataclass(frozen=True)
class ClientMasterSnapshot:
    """既存Notion取引先マスターDBの1ページから抽出した、名寄せに必要な最小限の情報。"""

    page_id: str
    title: str
    postal_code: str | None
    prefecture: str | None
    address: str | None


def fetch_client_master_snapshots(client: HttpNotionClient) -> list[ClientMasterSnapshot]:
    """取引先マスターDBの全ページから、名寄せに必要なプロパティのみを抽出する。

    `query_all_pages()`は生のNotionページオブジェクト全件（rollup/button等、
    `parse_notion_property_value()`が非対応の型を含む）を返すため、名寄せに必要な
    4プロパティ（取引先名・郵便番号・都道府県・住所、いずれも対応済みのtitle/rich_text/
    select型）のみを個別に取り出す（全プロパティを機械的に変換するとrollup等で
    ValueErrorになるため）。
    """
    snapshots: list[ClientMasterSnapshot] = []
    for page in client.query_all_pages():
        props = page.get("properties") or {}
        title = parse_notion_property_value(props["取引先名"]) if "取引先名" in props else None
        if not title:
            continue
        snapshots.append(
            ClientMasterSnapshot(
                page_id=page["id"],
                title=title,
                postal_code=(
                    parse_notion_property_value(props["郵便番号"]) if "郵便番号" in props else None
                ),
                prefecture=(
                    parse_notion_property_value(props["都道府県"]) if "都道府県" in props else None
                ),
                address=parse_notion_property_value(props["住所"]) if "住所" in props else None,
            )
        )
    return snapshots


@dataclass(frozen=True)
class ClientMatchIndex:
    """会社名での突合用に事前構築したインデックス（第一段階=単純正規化、第二段階=強め正規化）。"""

    basic: dict[str, ClientMasterSnapshot]
    strong: dict[str, list[ClientMasterSnapshot]]


def build_client_match_index(snapshots: list[ClientMasterSnapshot]) -> ClientMatchIndex:
    """既存Notionページのスナップショット群から、突合用インデックスを構築する。

    同一の正規化キーを持つ既存ページが複数ある場合（Notion側の重複登録等）、
    basicインデックスは先勝ちで1件のみ保持する（曖昧な状態のまま自動確定しないよう、
    strongインデックス側は全件保持し、match_existing_client()で複数件ヒットを検知できる
    ようにする）。
    """
    basic: dict[str, ClientMasterSnapshot] = {}
    strong: dict[str, list[ClientMasterSnapshot]] = {}
    for snap in snapshots:
        basic_key = normalize_company_name_basic(snap.title)
        if basic_key not in basic:
            basic[basic_key] = snap
        strong_key = normalize_company_name_strong(snap.title)
        strong.setdefault(strong_key, []).append(snap)
    return ClientMatchIndex(basic=basic, strong=strong)


def _postal_codes_conflict(a: str | None, b: str | None) -> bool:
    """郵便番号が両方存在し、明らかに異なる場合のみTrue（誤結合検知用の副次シグナル）。

    ハイフンの有無・「〒」記号等の表記ゆれは無視し、数字のみを比較する。
    """
    if not a or not b:
        return False
    digits_a = "".join(c for c in a if c.isdigit())
    digits_b = "".join(c for c in b if c.isdigit())
    if not digits_a or not digits_b:
        return False
    return digits_a != digits_b


@dataclass(frozen=True)
class ClientMatchResult:
    """突合結果。

    matchedがNoneの場合は「一致なし＝新規作成候補」。needs_reviewがTrueの場合は
    「一致した（または複数候補があった）が副次シグナルが食い違う・曖昧なため自動確定せず
    要確認」を表す（このケースではNotionへは書き込まず、人がレビューするレポートにのみ
    出力する）。
    """

    matched: ClientMasterSnapshot | None
    needs_review: bool
    reason: str | None = None


def match_existing_client(
    zoho_name: str | None,
    zoho_postal_code: str | None,
    index: ClientMatchIndex,
) -> ClientMatchResult:
    """Zoho取引先1件を、既存Notion取引先マスターの中から探す。

    第一段階（前後空白除去のみ）で一致すればそれを採用。無ければ第二段階
    （全角半角統一・法人格表記ゆれ吸収）を試す。第二段階で複数件にマッチする場合や、
    郵便番号が明らかに食い違う場合は、自動では確定せず要確認として返す。
    """
    basic_key = normalize_company_name_basic(zoho_name)
    basic_match = index.basic.get(basic_key)
    if basic_match is not None:
        if _postal_codes_conflict(zoho_postal_code, basic_match.postal_code):
            return ClientMatchResult(
                matched=basic_match,
                needs_review=True,
                reason="company name matched but postal code conflicts",
            )
        return ClientMatchResult(matched=basic_match, needs_review=False)

    strong_key = normalize_company_name_strong(zoho_name)
    strong_matches = index.strong.get(strong_key) or []
    if len(strong_matches) == 1:
        candidate = strong_matches[0]
        if _postal_codes_conflict(zoho_postal_code, candidate.postal_code):
            return ClientMatchResult(
                matched=candidate,
                needs_review=True,
                reason="normalized name matched but postal code conflicts",
            )
        return ClientMatchResult(matched=candidate, needs_review=False)
    if len(strong_matches) > 1:
        return ClientMatchResult(
            matched=None,
            needs_review=True,
            reason=f"normalized name matched {len(strong_matches)} existing records ambiguously",
        )

    return ClientMatchResult(matched=None, needs_review=False)
