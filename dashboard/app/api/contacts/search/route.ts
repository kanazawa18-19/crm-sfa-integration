import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, getErrorMessage, searchContacts } from "@/lib/backend";
import { getCurrentUser } from "@/lib/auth";

export async function GET(request: NextRequest) {
  // 連絡先DBの氏名検索であり、`/api/clients/search`と同様に機微度の高いデータ範囲に
  // 及ぶため、このルートも新規にセッションチェックを追加する
  // （shirokuma-secレビューWARN対応、2026-08-18に合わせた作り）。
  const user = await getCurrentUser();
  if (!user) {
    return NextResponse.json({ detail: "ログインが必要です" }, { status: 401 });
  }

  const q = request.nextUrl.searchParams.get("q") ?? "";

  try {
    const result = await searchContacts(q);
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
