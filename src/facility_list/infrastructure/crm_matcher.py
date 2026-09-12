"""施設とCRMを名前・住所・電話で照合する。

住所/電話だけの候補は新規リストから除外するが、連絡先は自動開示しない。
二次項目の取り込み途中・取得失敗・時間切れはNOT_CHECKEDとして返す。
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
from src.facility_list.domain.identity import normalize_address, normalize_phone
from src.facility_list.infrastructure.db import (
    ClientIdentityIndex, find_client_identity_candidates, find_client_pages_by_normalized_names,
)
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


def find_by_normalized_name(name: str) -> list[dict[str, Any]]:
    """単発照合も時間制限のある検索を通す。"""
    return find_client_pages_by_normalized_names([name]).get(name, [])


class _BudgetExpired(Exception):
    """共有の照合時間を使い切った。"""


class CrmMatcher:
    """施設とCRMの突合。Notionの読み取りは必要な分だけ行う。

    住所・電話は同期済みミラーを一括検索し、連絡先は確定した取引先だけ取得する。
    同じ取引先の再取得を避けるため、キャッシュは1回の突合内で共有する。

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
        self.unchecked_count = 0
        self._client_cache: dict[str, dict[str, Any]] = {}
        self._contacts_by_client: dict[str, tuple[CrmContact, ...]] = {}
        # `match_all()`が先にまとめて引いた結果。Noneなら1件ずつ引く(単発の`match()`用)。
        self._name_index: dict[str, list[dict[str, str]]] | None = None
        self._identity_index: ClientIdentityIndex | None = None
        self._batch = False

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
            "client_master", CLIENT_MASTER_SCHEMA.notion_database_id, api_key=self._api_key,
            **self._request_limits(),
        )

    def _contact_db(self) -> HttpNotionClient:
        return HttpNotionClient("contact", CONTACT_SCHEMA.notion_database_id,
                                api_key=self._api_key, **self._request_limits())

    def _request_limits(self) -> dict[str, Any]:
        self._check_budget()
        remaining = (self._deadline - time.monotonic()) if self._deadline is not None else 10.0
        if remaining <= 0:
            raise _BudgetExpired
        # 再試行の待機が時間予算を超えないよう、この用途に限り自動再試行しない。
        return {"timeout": min(10.0, remaining), "max_retries": 0, "max_rate_limit_retries": 0}

    def _check_budget(self) -> None:
        if self._budget_exhausted():
            raise _BudgetExpired

    def _load_contacts(self, page_id: str) -> tuple[CrmContact, ...]:
        """確定した取引先だけ取得する。全連絡先の読み込みで予算を消費しない。"""
        if not self._api_key:
            return ()
        if page_id in self._contacts_by_client:
            return self._contacts_by_client[page_id]
        contacts: list[CrmContact] = []
        body: dict[str, Any] = {"page_size": 100, "filter": {
            "property": "取引先マスター", "relation": {"contains": page_id}}}
        seen: set[str] = set()
        while True:
            self._check_budget()
            data = self._contact_db().query_raw(body)
            self._check_budget()
            for page in data["results"]:
                props = page.get("properties") or {}
                contacts.append(CrmContact(
                    contact_page_id=page["id"], name=_plain_text(props.get("名前")),
                    email=_plain_text(props.get("メールアドレス")),
                    phone=_plain_text(props.get("直通TEL")) or _plain_text(props.get("携帯番号")),
                    title=_plain_text(props.get("役職")),
                ))
            if data.get("has_more") is False:
                self._contacts_by_client[page_id] = tuple(contacts)
                return tuple(contacts)
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen or len(contacts) >= 10000:
                raise ValueError("連絡先の全件取得を確認できない")
            seen.add(cursor)
            body["start_cursor"] = cursor

    def _load_client_page(self, page_id: str) -> dict[str, Any]:
        if page_id in self._client_cache:
            return self._client_cache[page_id]
        if not self._api_key:
            self._client_cache[page_id] = {}
            return {}
        self._check_budget()
        page = self._client_master().get_raw_page(page_id)
        self._check_budget()
        if not page or page.get("archived") or page.get("in_trash") or not page.get("properties"):
            raise ValueError("取引先の現行ページを確認できない")
        self._client_cache[page_id] = page
        return page

    # -- 突合 --------------------------------------------------------------
    def match(self, facility: Facility) -> CrmMatch:
        """単発でも一括と同じ時間予算・取得失敗の扱いを使う。"""
        if not self._batch:
            self._deadline = time.monotonic() + self._time_budget
            self._identity_index = None
            self._name_index = None
            self._client_cache.clear()
            self._contacts_by_client.clear()
            self.skipped_by_budget = 0
        try:
            result = self._match(facility)
            self._check_budget()
            return result
        except _BudgetExpired:
            self.skipped_by_budget += 1
            return CrmMatch(state=CrmMatchState.NOT_CHECKED)
        except Exception:  # 取得失敗は営業対象にしない。個人情報やレスポンス本文はログに残さない。
            logger.warning("CRM突合を完了できなかった: hotelNo=%s", facility.hotel_no)
            return CrmMatch(state=CrmMatchState.NOT_CHECKED)

    def _prepare_identity(self, facilities: list[Facility]) -> None:
        self._check_budget()
        try:
            self._identity_index = find_client_identity_candidates(
                [key for f in facilities if (key := normalize_address(f.address, f.prefecture))],
                [key for f in facilities if (key := normalize_phone(f.telephone))],
            )
        except Exception:
            logger.warning("住所・電話の照合用ミラーを読み取れなかった")
            self._identity_index = ClientIdentityIndex()
        self._check_budget()

    def _match(self, facility: Facility) -> CrmMatch:
        self._check_budget()
        if self._identity_index is None:
            self._prepare_identity([facility])
        candidates: list[dict[str, Any]] = []
        strength = NameMatchStrength.EXACT
        for variant, variant_strength in facility_name_variants(facility.name):
            self._check_budget()
            normalized = normalize_company_name_strong(variant)
            if not normalized:
                continue
            if self._name_index is not None:
                hits = self._name_index.get(normalized, [])
            else:
                hits = find_by_normalized_name(normalized)
            self._check_budget()
            if hits:
                candidates = hits
                strength = variant_strength
                break

        address = normalize_address(facility.address, facility.prefecture)
        phone = normalize_phone(facility.telephone)
        secondary = [row for row in self._identity_index.rows if
                     (address and row["address"] == address) or (phone and row["phone"] == phone)]
        if secondary:
            # 別々の根拠が別会社を指すときは交差を都合よく選ばない。
            all_hits = {h["notion_page_id"]: h for h in [*candidates, *secondary]}
            hit = secondary[0]
            evidence = tuple(label for key, value, label in
                             (("address", address, "住所一致"), ("phone", phone, "電話一致"))
                             if value and hit[key] == value)
            conflicting = ((address and hit["address"] and address != hit["address"]) or
                           (phone and hit["phone"] and phone != hit["phone"]))
            if len(all_hits) != 1 or not self._identity_index.complete or conflicting:
                reasons = tuple(sorted(
                    {f"{r['raw_name']}：名前一致" for r in candidates}
                    | {f"{r['raw_name']}：{label}" for r in secondary
                     for key, value, label in (("address", address, "住所一致"), ("phone", phone, "電話一致"))
                     if value and r[key] == value}
                ))
                return CrmMatch(state=CrmMatchState.AMBIGUOUS,
                                candidate_names=tuple(h["raw_name"] for h in all_hits.values()),
                                evidence=reasons + (("候補間または登録値に矛盾",) if len(all_hits) > 1 or conflicting else ("二次項目の同期未完了",)))
            # 名前の裏付けがない住所単独・電話単独は同居会社や代表電話の可能性が残る。
            if len(evidence) < 2 and not candidates:
                return CrmMatch(state=CrmMatchState.AMBIGUOUS,
                                candidate_names=(hit["raw_name"],), evidence=evidence)
            # 二次キーが一致していても、Notion上の現行値を確かめる。
            if not self._api_key:
                return CrmMatch(state=CrmMatchState.NOT_CHECKED)
            page = self._load_client_page(hit["notion_page_id"])
            props = page.get("properties") or {}
            current_address = normalize_address(_plain_text(props.get("住所")), _plain_text(props.get("都道府県")))
            current_phone = normalize_phone(_plain_text(props.get("TEL")))
            if (("住所一致" in evidence and current_address != address) or
                    ("電話一致" in evidence and current_phone != phone) or
                    (address and current_address and address != current_address) or
                    (phone and current_phone and phone != current_phone)):
                return CrmMatch(state=CrmMatchState.AMBIGUOUS,
                                candidate_names=(hit["raw_name"],), evidence=("CRM更新差異・要確認",))
            # WEAKの都道府県照合はこの後も必ず通す。
            return self._resolved_match(facility, [hit], strength if candidates else None, evidence)

        if not candidates:
            # 追加項目が未取り込み・古い・施設側の照合材料なしなら不一致と断定しない。
            state = (CrmMatchState.NO_NAME_MATCH if self._identity_index.complete and (address or phone)
                     else CrmMatchState.NOT_CHECKED)
            return CrmMatch(state=state, evidence=("名前・登録済み住所/電話で一致なし",) if state is CrmMatchState.NO_NAME_MATCH else ())
        return self._resolved_match(facility, candidates, strength, ("名前一致",))

    def _resolved_match(
        self, facility: Facility, candidates: list[dict[str, Any]],
        strength: NameMatchStrength | None, evidence: tuple[str, ...],
    ) -> CrmMatch:
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
                # 弱い候補を住所で裏付けられない。新規には戻さず人の確認へ回す。
                return CrmMatch(state=CrmMatchState.AMBIGUOUS,
                                candidate_names=tuple(h["raw_name"] for h in candidates))
            else:
                return CrmMatch(
                    state=CrmMatchState.AMBIGUOUS,
                    candidate_names=tuple(h["raw_name"] for h in (narrowed or candidates)),
                )

        hit = candidates[0]
        page_id = hit["notion_page_id"]
        page = self._load_client_page(page_id)
        props = page.get("properties") or {}

        contacts = self._load_contacts(page_id)

        return CrmMatch(
            state=CrmMatchState.MATCHED,
            matched_by=strength,
            evidence=evidence,
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
        self.unchecked_count = 0
        self._batch = True
        self._client_cache.clear()
        self._contacts_by_client.clear()
        self._identity_index = None
        try:
            self._prepare_identity(facilities)
        except _BudgetExpired:
            self._identity_index = ClientIdentityIndex()
        # 名前の候補は先に全部分かるので、1回のクエリでまとめて引く
        # (1件ずつ新しい接続を開くと500施設で最大1,500接続になる)。
        all_names = [
            normalize_company_name_strong(variant)
            for facility in facilities
            for variant, _ in facility_name_variants(facility.name)
        ]
        try:
            self._check_budget()
            self._name_index = find_client_pages_by_normalized_names(all_names)
            self._check_budget()
        except Exception:
            # 失敗したDBへ件数分の再接続をしない。0件という正常結果にも置き換えない。
            self.unchecked_count = len(facilities)
            if self._budget_exhausted():
                self.skipped_by_budget = len(facilities)
            self._batch = False
            logger.warning("名前の一括照合を完了できなかった: %d施設", len(facilities))
            return {f.hotel_no: CrmMatch(state=CrmMatchState.NOT_CHECKED) for f in facilities}

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
            "CRM突合: 一致%d件 / 要確認%d件 / 登録情報で一致なし%d件 / 突合できず%d件",
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
        self.unchecked_count = failed
        self._batch = False
        return results
