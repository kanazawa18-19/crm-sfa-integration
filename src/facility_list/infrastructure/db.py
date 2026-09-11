"""`RakutenFacility`/`FacilityCheckInMachine`テーブルへの直接アクセス(2026-09-11)。

`src/project_mirror/db.py`・`src/relation_sync/db.py`と同じ方針: スキーマ管理は
dashboard(Next.js)側のPrismaに一本化しており、ここではraw SQLで読み書きするのみで
マイグレーションは行わない。接続文字列はdashboard側と同じ`DATABASE_URL`を共有する。

**mark-and-sweepは使わない。** 楽天から消えた施設も行は消さず`isListed`をfalseにする
(ProjectMirror全消失インシデントの教訓。母集団は消すより残す方が安い)。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.db_utils import db_truncated_utcnow
from src.facility_list.domain.models import (
    CategoryConfidence,
    CheckInMachineSource,
    CheckInMachineStatus,
    CustomPageStatus,
    Facility,
    FacilityCategory,
)

logger = logging.getLogger(__name__)

_UPSERT_BATCH_SIZE = 500


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/project_mirror/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def _chunked(records: list[Any], size: int) -> list[list[Any]]:
    return [records[i : i + size] for i in range(0, len(records), size)]


def upsert_facilities(
    facilities: list[Facility], *, parse_warnings: dict[int, str] | None = None
) -> int:
    """施設をまとめてUPSERTする。`hotelNo`が一致する行は上書きする。

    **チェックイン機の判定結果(`FacilityCheckInMachine`)には触れない。** 別テーブルに
    分けてあるのは、施設マスタを取り込み直してもWEB検索の結果を失わないようにするため。
    """
    if not facilities:
        return 0

    warnings = parse_warnings or {}
    now = db_truncated_utcnow()
    written = 0

    with _connect() as conn:
        for batch in _chunked(facilities, _UPSERT_BATCH_SIZE):
            rows = [
                (
                    uuid.uuid4().hex,  # project_mirror/db.pyと同じ採番(ハイフン無し)
                    f.hotel_no,
                    f.name,
                    f.postal_code,
                    f.prefecture,
                    f.city,
                    f.address,
                    f.room_count,
                    f.review_average,
                    f.review_count,
                    f.photo_count,
                    f.min_charge,
                    f.category.value,
                    f.category_confidence.value,
                    f.has_onsen,
                    f.custom_page_status.value,
                    f.custom_page_count,
                    json.dumps(list(f.facilities), ensure_ascii=False),
                    json.dumps(list(f.room_facilities), ensure_ascii=False),
                    f.is_listed,
                    now,
                    warnings.get(f.hotel_no),
                    now,
                    now,
                )
                for f in batch
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO "RakutenFacility" (
                        "id", "hotelNo", "name", "postalCode", "prefecture", "city", "address",
                        "roomCount", "reviewAverage", "reviewCount", "photoCount", "minCharge",
                        "category", "categoryConfidence", "hasOnsen",
                        "customPageStatus", "customPageCount",
                        "facilities", "roomFacilities", "isListed",
                        "fetchedAt", "parseWarning", "createdAt", "updatedAt"
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s::"FacilityCategory", %s::"FacilityCategoryConfidence", %s,
                        %s::"CustomPageStatus", %s,
                        %s::jsonb, %s::jsonb, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT ("hotelNo") DO UPDATE SET
                        "name" = EXCLUDED."name",
                        "postalCode" = EXCLUDED."postalCode",
                        "prefecture" = EXCLUDED."prefecture",
                        "city" = EXCLUDED."city",
                        "address" = EXCLUDED."address",
                        "roomCount" = EXCLUDED."roomCount",
                        "reviewAverage" = EXCLUDED."reviewAverage",
                        "reviewCount" = EXCLUDED."reviewCount",
                        "photoCount" = EXCLUDED."photoCount",
                        "minCharge" = EXCLUDED."minCharge",
                        "category" = EXCLUDED."category",
                        "categoryConfidence" = EXCLUDED."categoryConfidence",
                        "hasOnsen" = EXCLUDED."hasOnsen",
                        "customPageStatus" = EXCLUDED."customPageStatus",
                        "customPageCount" = EXCLUDED."customPageCount",
                        "facilities" = EXCLUDED."facilities",
                        "roomFacilities" = EXCLUDED."roomFacilities",
                        "isListed" = EXCLUDED."isListed",
                        "fetchedAt" = EXCLUDED."fetchedAt",
                        "parseWarning" = EXCLUDED."parseWarning",
                        "updatedAt" = EXCLUDED."updatedAt"
                    """,
                    rows,
                )
                written += len(rows)
        conn.commit()

    logger.info("施設を%d件UPSERTした", written)
    return written


def mark_unlisted(hotel_nos: list[int]) -> int:
    """掲載終了(ページが404)だった施設に印を付ける。**行は消さない。**"""
    if not hotel_nos:
        return 0
    now = db_truncated_utcnow()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "RakutenFacility"
                   SET "isListed" = false, "updatedAt" = %s
                 WHERE "hotelNo" = ANY(%s) AND "isListed" = true
                """,
                (now, hotel_nos),
            )
            updated = cur.rowcount
        conn.commit()
    logger.info("掲載終了として印を付けた施設: %d件", updated)
    return updated


def upsert_check_in_machine(
    hotel_no: int,
    *,
    status: CheckInMachineStatus,
    source: CheckInMachineSource,
    evidence: str | None = None,
    evidence_url: str | None = None,
) -> None:
    """チェックイン機の判定結果を記録する。根拠(`evidence`)を必ず残す。"""
    now = db_truncated_utcnow()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "FacilityCheckInMachine" (
                    "hotelNo", "status", "source", "evidence", "evidenceUrl",
                    "checkedAt", "updatedAt"
                ) VALUES (
                    %s, %s::"CheckInMachineStatus", %s::"CheckInMachineSource", %s, %s, %s, %s
                )
                ON CONFLICT ("hotelNo") DO UPDATE SET
                    "status" = EXCLUDED."status",
                    "source" = EXCLUDED."source",
                    "evidence" = EXCLUDED."evidence",
                    "evidenceUrl" = EXCLUDED."evidenceUrl",
                    "checkedAt" = EXCLUDED."checkedAt",
                    "updatedAt" = EXCLUDED."updatedAt"
                """,
                (hotel_no, status.value, source.value, evidence, evidence_url, now, now),
            )
        conn.commit()


def _row_to_facility(row: dict[str, Any]) -> Facility:
    return Facility(
        hotel_no=row["hotelNo"],
        name=row["name"],
        address=row["address"],
        postal_code=row["postalCode"],
        prefecture=row["prefecture"],
        city=row["city"],
        room_count=row["roomCount"],
        review_average=row["reviewAverage"],
        review_count=row["reviewCount"],
        photo_count=row["photoCount"],
        min_charge=row["minCharge"],
        category=FacilityCategory(row["category"]),
        category_confidence=CategoryConfidence(row["categoryConfidence"]),
        has_onsen=row["hasOnsen"],
        custom_page_status=CustomPageStatus(row["customPageStatus"]),
        custom_page_count=row["customPageCount"],
        check_in_machine=CheckInMachineStatus(row.get("checkInStatus") or "unknown"),
        check_in_machine_source=CheckInMachineSource(row.get("checkInSource") or "none"),
        facilities=tuple(row["facilities"] or ()),
        room_facilities=tuple(row["roomFacilities"] or ()),
        is_listed=row["isListed"],
    )


def fetch_facilities(
    *, prefectures: list[str] | None = None, only_listed: bool = True, limit: int | None = None
) -> list[Facility]:
    """条件の一次絞り込み(都道府県)だけDBで行い、残りはドメイン層で判定する。

    都道府県で絞れば1回あたり数百〜数千件に収まるため、細かい条件までSQLへ持ち込まず
    `matches_criteria()`に任せる(業務ルールをSQLに散らさないため)。
    """
    where = []
    params: list[Any] = []
    if only_listed:
        where.append('f."isListed" = true')
    if prefectures:
        where.append('f."prefecture" = ANY(%s)')
        params.append(prefectures)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %s"
        params.append(limit)

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT f.*, c."status" AS "checkInStatus", c."source" AS "checkInSource"
                  FROM "RakutenFacility" f
                  LEFT JOIN "FacilityCheckInMachine" c ON c."hotelNo" = f."hotelNo"
                  {clause}
                 ORDER BY f."hotelNo"
                 {limit_clause}
                """,
                params,
            )
            rows = cur.fetchall()
    return [_row_to_facility(r) for r in rows]


def count_facilities() -> dict[str, int]:
    """取り込み状況の確認用。件数だけを返す。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE "isListed") AS listed,
                       COUNT(*) FILTER (WHERE "roomCount" IS NULL) AS missing_room_count,
                       COUNT(*) FILTER (WHERE "parseWarning" IS NOT NULL) AS with_warning
                  FROM "RakutenFacility"
                """
            )
            row = cur.fetchone() or {}
    return {k: int(v or 0) for k, v in row.items()}


def find_client_pages_by_normalized_names(
    normalized_names: list[str],
) -> dict[str, list[dict[str, str]]]:
    """正規化済み取引先名をまとめて`ClientNameIndex`から引く。

    `src/relation_sync/db.py`の`find_by_normalized_name()`は1件ごとに新しい接続を
    開くため、500施設×名前候補3つで最大1,500回の接続になる
    (shirokuma-secレビュー指摘、2026-09-11)。突合では名前の候補が先に全部分かる
    ので、1回の接続・1本のクエリでまとめて引く。

    戻り値は正規化名をキーにした辞書。同じ正規化キーに複数の取引先が載ることが
    あるためリストで返す(曖昧なものを自動確定させないのは呼び出し元の責務)。
    """
    unique = sorted({name for name in normalized_names if name})
    if not unique:
        return {}

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT "normalizedName", "notionPageId", "rawName"
                  FROM "ClientNameIndex"
                 WHERE "normalizedName" = ANY(%s)
                """,
                (unique,),
            )
            rows = cur.fetchall()

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["normalizedName"], []).append(
            {"notion_page_id": row["notionPageId"], "raw_name": row["rawName"]}
        )
    return grouped


def record_list_run(
    *,
    user_id: str,
    criteria: dict[str, Any],
    total_count: int,
    matched_count: int,
    new_count: int,
    ambiguous_count: int,
    output_url: str | None = None,
) -> str | None:
    """リストを1本作ったことを`FacilityListRun`に残す。

    「先週と同じ条件でもう1回」が現場で必ず起きるので、条件をそのまま保存する。
    **記録に失敗しても書き出し自体は止めない**（履歴が残らないことより、
    作ったリストが手元に来ないことの方が困る）。失敗したらNoneを返す。
    """
    run_id = uuid.uuid4().hex
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO "FacilityListRun" (
                        "id", "userId", "criteria", "totalCount", "matchedCount",
                        "newCount", "ambiguousCount", "outputUrl", "createdAt"
                    ) VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        user_id,
                        json.dumps(criteria, ensure_ascii=False, default=str),
                        total_count,
                        matched_count,
                        new_count,
                        ambiguous_count,
                        output_url,
                        db_truncated_utcnow(),
                    ),
                )
            conn.commit()
    except Exception:  # noqa: BLE001 - 履歴が残せなくても書き出しは通す
        logger.exception("リスト作成の履歴を残せなかった: userId=%s", user_id)
        return None
    return run_id


@dataclass(frozen=True)
class ClientNameIndexHealth:
    """`ClientNameIndex`(CRM取引先名のローカルミラー)の状態。

    新規開拓リストは「名前で当たらなかった」ことを根拠にする。ミラーが空だったり
    古かったりすると、**正常に検索できてしまうので例外にもならず**、既存顧客が
    丸ごと新規リストに載る(ChatGPTレビュー指摘、2026-09-12)。
    書き出しの前にここを見て、怪しければ止める。
    """

    row_count: int
    last_synced_at: datetime | None

    def is_fresh(self, *, max_age_hours: float = 48.0, min_rows: int = 1000) -> bool:
        if self.row_count < min_rows:
            return False
        if self.last_synced_at is None:
            return False
        age = datetime.now(timezone.utc) - self.last_synced_at
        return age <= timedelta(hours=max_age_hours)

    def describe(self) -> str:
        when = self.last_synced_at.strftime("%Y-%m-%d %H:%M UTC") if self.last_synced_at else "不明"
        return f"取引先名インデックス {self.row_count}件 / 最終同期 {when}"


def client_name_index_health() -> ClientNameIndexHealth:
    """`ClientNameIndex`の件数と最終同期日時を読む。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT COUNT(*) AS n, MAX("syncedAt") AS last FROM "ClientNameIndex"'
            )
            row = cur.fetchone() or {}
    last = row.get("last")
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return ClientNameIndexHealth(row_count=int(row.get("n") or 0), last_synced_at=last)
