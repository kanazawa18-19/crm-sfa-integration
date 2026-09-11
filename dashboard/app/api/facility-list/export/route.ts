import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, exportFacilityList, getErrorMessage } from "@/lib/backend";
import { normalizeCriteria, requireFacilityListEditor } from "@/lib/facilityListApiAuth";

// CRMと突合した完全なリストをCSVで返す(2026-09-11)。
//
// 作成者はクライアントの申告ではなくログイン中のユーザーから取る
// (他人の名前でリストを作らせない。一斉配信のプレビューと同じ方針)。
export async function POST(request: NextRequest) {
  const auth = await requireFacilityListEditor();
  if (auth.error) return auth.error;
  const user = auth.user;

  const payload = await request.json().catch(() => null);
  if (!payload || typeof payload !== "object") {
    return NextResponse.json({ detail: "リクエストの形式が不正です" }, { status: 400 });
  }

  try {
    const result = await exportFacilityList({
      ...normalizeCriteria(payload as Record<string, unknown>),
      created_by: user.name ?? user.email ?? "",
      // 履歴(FacilityListRun)の実行者。クライアントの申告ではなく
      // ログイン中のユーザーから取る。
      user_id: user.id,
    });
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
