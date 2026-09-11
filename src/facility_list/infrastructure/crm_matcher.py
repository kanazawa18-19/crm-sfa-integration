"""楽天トラベルの施設をCRM(Notion取引先マスターDB)と突合する(2026-09-11)。

照合の材料は**施設名と住所だけ**である。CRMに登録されているのは運営会社名のことが
多く(「株式会社◯◯」が「ホテル△△」を運営している)、施設名だけでは結び付かない例が
必ず残る。取りこぼしを「未取引」と言い切らないため、結果は3値で返す:

    MATCHED        1件に確定した
    AMBIGUOUS      候補が複数ある(人の確認が要る)。**新規リストには載せない**
    NO_NAME_MATCH  名前照合では当たらなかった。**「未取引」とは言い切れない**
    NOT_CHECKED    突合そのものができなかった(DB障害等)

**`NO_NAME_MATCH`を「未取引」と読み替えないこと。** CRMには運営会社名で登録されて
いることが多く、施設名だけでは既存顧客でも当たらない。住所・電話での二次照合は
未実装(他社レビュー指摘、2026-09-12)。

名前の一次照合は`ClientNameIndex`(Notion取引先マスターのローカルミラー)への完全一致で
行う。`src/relation_sync/resolve.py`と同じ正規化関数を使い、二重実装を避ける。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.contact import CONTACT_SCHEMA
from src.facility_list.domain.models import (
    CrmContact,
    CrmMatch,
    CrmMatchState,
    Facility,
    NameMatchStrength,
)
from src.migration.zoho_client_master import normalize_company_name_strong
from src.facility_list.infrastructure.db import find_client_pages_by_normalized_names
from src.relation_sync.db import find_by_normalized_name
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

# 施設名の頭に付く地名・温泉地名を落とすための区切り。楽天の施設名は
# 「皆生温泉　皆生グランドホテル天水」のように、全角スペースで地名と施設名が
# 分かれていることが多い(2026-09-11、鳥取県の実データで確認)。
_NAME_SEPARATOR_RE = re.compile(r"[\s　]+")

# 施設名の先頭に付きやすい修飾。落としてから再照合する。
_NAME_PREFIXES = ("天然温泉", "源泉かけ流し", "貸切風呂", "全室", "新館", "本館")


def facility_name_variants(name: str) -> list[tuple[str, NameMatchStrength]]:
    """照合に使う施設名の候補を、確からしい順に(候補, 強さ)で返す。

    1. そのまま／空白を全部落としたもの      → EXACT
    2. 先頭の修飾語を落としたもの            → STRIPPED
    3. 空白で割った最後の塊                  → WEAK（単独では確定させない）

    3を単独で信用すると、「ホテルABC 大阪」の「大阪」が別会社に1件だけ当たって、
    **その会社の担当者名・メール・電話が別施設の行に出る**
    (ChatGPTレビューのBLOCKER、2026-09-12)。呼び出し側は`WEAK`のときだけ
    住所の一致を必須にすること。
    """
    variants: list[tuple[str, NameMatchStrength]] = []
    seen: set[str] = set()

    def add(value: str, strength: NameMatchStrength) -> None:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            variants.append((value, strength))

    add(name, NameMatchStrength.EXACT)

    parts = [p for p in _NAME_SEPARATOR_RE.split(name) if p]
    if len(parts) > 1:
        # 空白を詰めただけのものは元の名前と同じ強さでよい。
        add("".join(parts), NameMatchStrength.EXACT)

    # 修飾語は連続しうる(「天然温泉源泉かけ流し◯◯」)ので、落とせなくなるまで回す
    # (Geminiレビュー指摘、2026-09-12。startswithのif1回だと先頭1つしか落ちない)。
    stripped = name
    while True:
        for prefix in _NAME_PREFIXES:
            if stripped.startswith(prefix) and len(stripped) > len(prefix):
                stripped = stripped[len(prefix) :].lstrip("　 ")
                break
        else:
            break
    add(stripped, NameMatchStrength.STRIPPED)

    if len(parts) > 1:
        add(parts[-1], NameMatchStrength.WEAK)
    return variants


def _plain_text(prop: dict[str, Any] | None) -> str | None:
    """Notionのプロパティ値からテキストを1つ取り出す(型ごとの差を吸収する)。"""
    if not prop:
        return None
    kind = prop.get("type")
    if kind == "title" or kind == "rich_text":
        parts = [t.get("plain_text", "") for t in prop.get(kind) or []]
        return "".join(parts).strip() or None
    if kind in ("email", "phone_number", "url"):
        value = prop.get(kind)
        return str(value).strip() if value else None
    if kind == "select":
        sel = prop.get("select")
        return (sel or {}).get("name")
    if kind == "rollup":
        rollup = prop.get("rollup") or {}
        if rollup.get("type") == "array":
            values = [_plain_text(item) for item in rollup.get("array") or []]
            joined = ", ".join(v for v in values if v)
            return joined or None
        return _plain_text(rollup)
    return None


def _multi_values(prop: dict[str, Any] | None) -> tuple[str, ...]:
    """ロールアップ・マルチセレクトから値の一覧を取り出す。

    「【営業部】提案済みサービス」はロールアップのため**Notion APIのfilterでは絞れない**
    ([[reference_kintone_get_content_type_cb_il02]]と同じ類の既知の制約)。読み取って
    こちら側で判定する。
    """
    if not prop:
        return ()
    kind = prop.get("type")
    if kind == "multi_select":
        return tuple(item.get("name", "") for item in prop.get("multi_select") or [] if item)
    if kind == "rollup":
        rollup = prop.get("rollup") or {}
        if rollup.get("type") == "array":
            values: list[str] = []
            for item in rollup.get("array") or []:
                text = _plain_text(item)
                if text:
                    values.extend(v.strip() for v in text.split(",") if v.strip())
                elif item.get("type") == "multi_select":
                    values.extend(m.get("name", "") for m in item.get("multi_select") or [])
            return tuple(dict.fromkeys(v for v in values if v))
    return ()


# CRM突合に使ってよい時間の既定値(秒)。Vercelの maxDuration は300秒で、
# 残りをCSV生成・応答に使う。**件数ではなく時間で区切る。**
#
# 「500件までなら300秒に収まる」には根拠が無かった(他社レビュー2社が独立に指摘、
# 2026-09-12)。Notionの個別ページ取得は1件あたり数百ms〜1秒かかり、全件が既存顧客
# なら500件で250秒を超えうる一方、全件が新規なら数秒で終わる。件数から時間は決まらない。
DEFAULT_TIME_BUDGET_SECONDS = 180.0


class CrmMatcher:
    """施設とCRMの突合。Notionの読み取りは必要な分だけ行う。

    連絡先は毎回1件ずつ引くと遅いので、最初に連絡先DBを1度だけ読み込んで
    取引先ごとにまとめておく(`NOTION_API_KEY`が無い環境では連絡先を付けずに動く)。

    **時間予算を持つ。** 予算を使い切ったら、残りの施設は`NOT_CHECKED`にして
    打ち切る。黙って遅延させて504にするより、「ここまでは調べた」と正直に返す方がよい。
    """

    def __init__(
        self,
        *,
        notion_api_key: str | None = None,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ) -> None:
        self._api_key = notion_api_key if notion_api_key is not None else os.environ.get("NOTION_API_KEY")
        self._time_budget = time_budget_seconds
        self._deadline: float | None = None
        # 予算切れで突合を打ち切った施設数。呼び出し元が画面に出す。
        self.skipped_by_budget = 0
        self._client_cache: dict[str, dict[str, Any]] = {}
        self._contacts_by_client: dict[str, list[CrmContact]] | None = None
        # `match_all()`が先にまとめて引いた結果。Noneなら1件ずつ引く(単発の`match()`用)。
        self._name_index: dict[str, list[dict[str, str]]] | None = None

    @property
    def has_notion_access(self) -> bool:
        """Notionを読めるか。

        キーが無くても`ClientNameIndex`(ローカルのミラー)への名前照合だけは動くため、
        コンストラクタは例外を投げない。その代わり**取引先の詳細・提案済みサービス・
        連絡先が取れない**ので、それらを使う条件を指定された呼び出し元は、ここを見て
        断ること(obasan-qualityレビュー指摘、2026-09-11)。
        """
        return bool(self._api_key)

    # -- Notion読み取り ----------------------------------------------------
    def _client_master(self) -> HttpNotionClient:
        return HttpNotionClient(
            "client_master", CLIENT_MASTER_SCHEMA.notion_database_id, api_key=self._api_key
        )

    def _contact_db(self) -> HttpNotionClient:
        return HttpNotionClient("contact", CONTACT_SCHEMA.notion_database_id, api_key=self._api_key)

    def _load_contacts(self) -> dict[str, list[CrmContact]]:
        """連絡先DBを1度だけ読み込み、取引先ページIDごとにまとめる。"""
        if self._contacts_by_client is not None:
            return self._contacts_by_client
        grouped: dict[str, list[CrmContact]] = {}
        if not self._api_key:
            logger.warning("NOTION_API_KEYが無いため連絡先は取得しない")
            self._contacts_by_client = grouped
            return grouped

        pages = self._contact_db().query_all_pages()
        for page in pages:
            props = page.get("properties") or {}
            relation = props.get("取引先マスター") or {}
            related_ids = [r.get("id") for r in relation.get("relation") or [] if r.get("id")]
            if not related_ids:
                continue
            contact = CrmContact(
                contact_page_id=page.get("id", ""),
                name=_plain_text(props.get("名前")),
                email=_plain_text(props.get("メールアドレス")),
                phone=_plain_text(props.get("直通TEL")) or _plain_text(props.get("携帯番号")),
                title=_plain_text(props.get("役職")),
            )
            for client_id in related_ids:
                grouped.setdefault(client_id, []).append(contact)
        logger.info("連絡先を%d取引先分読み込んだ", len(grouped))
        self._contacts_by_client = grouped
        return grouped

    def _load_client_page(self, page_id: str) -> dict[str, Any]:
        if page_id in self._client_cache:
            return self._client_cache[page_id]
        if not self._api_key:
            self._client_cache[page_id] = {}
            return {}
        page = self._client_master().get_raw_page(page_id) or {}
        self._client_cache[page_id] = page
        return page

    # -- 突合 --------------------------------------------------------------
    def match(self, facility: Facility) -> CrmMatch:
        """施設1軒をCRMと突合する。"""
        candidates: list[dict[str, Any]] = []
        strength = NameMatchStrength.EXACT
        for variant, variant_strength in facility_name_variants(facility.name):
            normalized = normalize_company_name_strong(variant)
            if not normalized:
                continue
            if self._name_index is not None:
                hits = self._name_index.get(normalized, [])
            else:
                hits = find_by_normalized_name(normalized)
            if hits:
                candidates = hits
                strength = variant_strength
                break

        if not candidates:
            # **NOT_FOUND(未取引)ではない。** 名前で当たらなかっただけ。
            return CrmMatch(state=CrmMatchState.NO_NAME_MATCH)

        # 弱い候補(空白区切りの最後の塊)は、1件しか当たらなくても確定させない。
        # 住所が一致して初めて同じ相手とみなす(ChatGPTレビューのBLOCKER、2026-09-12)。
        needs_address_proof = strength is NameMatchStrength.WEAK

        if len(candidates) > 1 or needs_address_proof:
            # 住所(都道府県)で絞る。取引先マスターの「都道府県」を読んで
            # 施設の都道府県と一致するものだけ残す。
            narrowed = []
            for hit in candidates:
                page = self._load_client_page(hit["notion_page_id"])
                props = page.get("properties") or {}
                pref = _plain_text(props.get("都道府県"))
                if needs_address_proof:
                    # 弱い候補では「都道府県が読めない」も根拠不十分として落とす。
                    if not pref or not facility.prefecture or pref != facility.prefecture:
                        continue
                elif pref and facility.prefecture and pref != facility.prefecture:
                    continue
                narrowed.append(hit)

            if len(narrowed) == 1:
                candidates = narrowed
            elif not narrowed and needs_address_proof:
                # 弱い候補しか無く、住所でも裏が取れなかった。当たらなかった扱いにする。
                return CrmMatch(state=CrmMatchState.NO_NAME_MATCH)
            else:
                return CrmMatch(
                    state=CrmMatchState.AMBIGUOUS,
                    candidate_names=tuple(h["raw_name"] for h in (narrowed or candidates)),
                )

        hit = candidates[0]
        page_id = hit["notion_page_id"]
        page = self._load_client_page(page_id)
        props = page.get("properties") or {}

        contacts = tuple(self._load_contacts().get(page_id, ()))

        return CrmMatch(
            state=CrmMatchState.MATCHED,
            matched_by=strength,
            client_page_id=page_id,
            client_name=hit["raw_name"],
            owner_name=_plain_text(props.get("担当者")),
            client_phone=_plain_text(props.get("TEL")) or _plain_text(props.get("電話番号")),
            client_fax=_plain_text(props.get("FAX")),
            proposed_services=_multi_values(props.get("【営業部】提案済みサービス")),
            contracted_services=_multi_values(props.get("サービス・商品")),
            contacts=contacts,
        )

    def _budget_exhausted(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def match_all(self, facilities: list[Facility]) -> dict[int, CrmMatch]:
        """施設をまとめて突合する。戻り値は施設番号をキーにした辞書。

        時間予算を使い切ったら、残りは`NOT_CHECKED`にして打ち切る。
        **打ち切った分を「未取引」にしない**(新規開拓リストに混ざるため)。
        """
        self._deadline = time.monotonic() + self._time_budget
        self.skipped_by_budget = 0
        # 名前の候補は先に全部分かるので、1回のクエリでまとめて引く
        # (1件ずつ新しい接続を開くと500施設で最大1,500接続になる)。
        all_names = [
            normalize_company_name_strong(variant)
            for facility in facilities
            for variant, _ in facility_name_variants(facility.name)
        ]
        try:
            self._name_index = find_client_pages_by_normalized_names(all_names)
        except Exception:  # noqa: BLE001 - 引けなければ1件ずつに戻すだけ
            logger.exception("取引先名インデックスの一括取得に失敗した。1件ずつ引く")
            self._name_index = None

        results: dict[int, CrmMatch] = {}
        for facility in facilities:
            if self._budget_exhausted():
                results[facility.hotel_no] = CrmMatch(state=CrmMatchState.NOT_CHECKED)
                self.skipped_by_budget += 1
                continue
            try:
                results[facility.hotel_no] = self.match(facility)
            except Exception:  # noqa: BLE001 - 1件の失敗で全体を止めない
                # **NOT_FOUNDにしない。** 突合できなかっただけの施設を「未取引」として
                # 返すと、Notionが一時的に落ちただけで既存顧客が新規開拓リストに
                # 混ざる(obasan-qualityレビュー指摘、2026-09-11)。
                logger.exception("CRM突合に失敗した: hotelNo=%s", facility.hotel_no)
                results[facility.hotel_no] = CrmMatch(state=CrmMatchState.NOT_CHECKED)
        matched = sum(1 for m in results.values() if m.state is CrmMatchState.MATCHED)
        ambiguous = sum(1 for m in results.values() if m.state is CrmMatchState.AMBIGUOUS)
        failed = sum(1 for m in results.values() if m.state is CrmMatchState.NOT_CHECKED)
        no_name = sum(1 for m in results.values() if m.state is CrmMatchState.NO_NAME_MATCH)
        logger.info(
            "CRM突合: 一致%d件 / 候補が複数%d件 / 名前で当たらず%d件 / 突合できず%d件",
            matched,
            ambiguous,
            no_name,
            failed,
        )
        if failed:
            # 黙って減らさない。新規リストに載らなかった施設が何件あるかを知らせる。
            logger.warning("突合できなかった施設が%d件ある。新規リストには載せていない", failed)
        if self.skipped_by_budget:
            logger.warning(
                "時間予算(%.0f秒)を使い切ったため%d件の突合を打ち切った",
                self._time_budget,
                self.skipped_by_budget,
            )
        return results
