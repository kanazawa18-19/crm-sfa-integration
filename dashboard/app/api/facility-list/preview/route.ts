import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, getErrorMessage, previewFacilityList } from "@/lib/backend";
import { normalizeCriteria, requireFacilityListEditor } from "@/lib/facilityListApiAuth";

// 抽出条件に当たる件数と先頭数十件を返す(2026-09-11)。
// CRM突合はしないので速い。条件を詰めている最中に使う。
export async function POST(request: NextRequest) {
  const auth = await requireFacilityListEditor();
  if (auth.error) return auth.error;

  const payload = await request.json().catch(() => null);
  if (!payload || typeof payload !== "object") {
    return NextResponse.json({ detail: "リクエストの形式が不正です" }, { status: 400 });
  }

  try {
    const result = await previewFacilityList(normalizeCriteria(payload as Record<string, unknown>));
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
