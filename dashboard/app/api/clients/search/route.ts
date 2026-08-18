import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, getErrorMessage, searchClients } from "@/lib/backend";
import { getCurrentUser } from "@/lib/auth";

export async function GET(request: NextRequest) {
  // 取引先マスターDB(実測約6.2万件)の会社名検索であり、既存の/api/projects/search
  // (未認証チェックなし)より機微度の高いデータ範囲に及ぶため、このルートは新規に
  // セッションチェックを追加する(shirokuma-secレビューWARN対応、2026-08-18)。
  const user = await getCurrentUser();
  if (!user) {
    return NextResponse.json({ detail: "ログインが必要です" }, { status: 401 });
  }

  const q = request.nextUrl.searchParams.get("q") ?? "";

  try {
    const result = await searchClients(q);
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
