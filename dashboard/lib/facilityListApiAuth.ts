import { NextResponse } from "next/server";
import { getCurrentUser, type CurrentUser } from "@/lib/auth";

// リスト作成マシーンのAPIルートの権限チェック(2026-09-11)。
//
// 閲覧者(viewer)を入れないのは、書き出したリストに取引先の担当者名・メールアドレス・
// 電話番号が並ぶため。プレビューも同じ扱いにする(件数だけでなく施設名まで返すため)。

export type FacilityListApiAuth =
  | { user: CurrentUser; error?: undefined }
  | { user?: undefined; error: NextResponse };

export async function requireFacilityListEditor(): Promise<FacilityListApiAuth> {
  const user = await getCurrentUser();
  if (!user) {
    return { error: NextResponse.json({ detail: "ログインが必要です" }, { status: 401 }) };
  }
  if (user.role === "viewer") {
    return {
      error: NextResponse.json(
        { detail: "この操作には編集者以上の権限が必要です" },
        { status: 403 }
      ),
    };
  }
  return { user };
}

/** 画面から来た条件を、バックエンドが受け取れる形に整える。 */
export function normalizeCriteria(payload: Record<string, unknown>) {
  const asStringArray = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  const asNumber = (value: unknown): number | null =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const asBool = (value: unknown): boolean | null =>
    typeof value === "boolean" ? value : null;

  return {
    prefectures: asStringArray(payload.prefectures),
    room_count_min: asNumber(payload.room_count_min) ?? 1,
    room_count_max: asNumber(payload.room_count_max),
    review_min: asNumber(payload.review_min),
    review_max: asNumber(payload.review_max),
    include_unrated: payload.include_unrated === true,
    categories: asStringArray(payload.categories),
    custom_page: typeof payload.custom_page === "string" ? payload.custom_page : null,
    check_in_machine:
      typeof payload.check_in_machine === "string" ? payload.check_in_machine : null,
    has_onsen: asBool(payload.has_onsen),
    min_review_count: asNumber(payload.min_review_count),
    max_photo_count: asNumber(payload.max_photo_count),
    crm_filter: typeof payload.crm_filter === "string" ? payload.crm_filter : "any",
    exclude_proposed_services: asStringArray(payload.exclude_proposed_services),
    exclude_chains: payload.exclude_chains === true,
    limit: asNumber(payload.limit),
  };
}
