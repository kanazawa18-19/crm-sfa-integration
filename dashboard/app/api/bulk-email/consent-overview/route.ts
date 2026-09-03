import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, bulkEmailConsentOverview, getErrorMessage } from "@/lib/backend";
import { requireBulkEmailEditor } from "@/lib/bulkEmailApiAuth";

// 送信根拠の登録画面が使う連絡先一覧(2026-09-03)。読み取りのみ。
//
// preview/route.ts と同じく、取引先の連絡先(氏名・メールアドレス)がまとめて返るため
// セッション必須・閲覧者(viewer)は不可にする。
export async function POST(request: NextRequest) {
  const auth = await requireBulkEmailEditor();
  if (auth.error) return auth.error;

  const payload = await request.json().catch(() => null);
  const clientPageIds = (payload as Record<string, unknown> | null)?.client_page_ids;
  if (!Array.isArray(clientPageIds)) {
    return NextResponse.json({ detail: "取引先が選択されていません" }, { status: 400 });
  }

  try {
    const result = await bulkEmailConsentOverview({
      client_page_ids: clientPageIds.filter((id): id is string => typeof id === "string"),
    });
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
