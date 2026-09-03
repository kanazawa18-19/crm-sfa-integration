import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, getErrorMessage, previewBulkEmail } from "@/lib/backend";
import { getCurrentUser } from "@/lib/auth";

// 一斉配信プレビューのブラウザ側入口(2026-09-03)。
//
// 取引先の連絡先(氏名・メールアドレス)がまとめて返るため、/api/clients/searchと同じ理由で
// セッションチェックを必須にする。加えて閲覧者(viewer)は使えない — 送信そのものはまだ
// 無いが、この画面は営業メールの下書きを作る場であり、閲覧専用の権限で入る場所ではない。
export async function POST(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) {
    return NextResponse.json({ detail: "ログインが必要です" }, { status: 401 });
  }
  if (user.role === "viewer") {
    return NextResponse.json({ detail: "この操作には編集者以上の権限が必要です" }, { status: 403 });
  }

  const payload = await request.json().catch(() => null);
  if (!payload || typeof payload !== "object") {
    return NextResponse.json({ detail: "リクエストの形式が不正です" }, { status: 400 });
  }

  const { subject, body, client_page_ids: clientPageIds } = payload as Record<string, unknown>;
  if (!Array.isArray(clientPageIds)) {
    return NextResponse.json({ detail: "取引先が選択されていません" }, { status: 400 });
  }

  try {
    const result = await previewBulkEmail({
      subject: typeof subject === "string" ? subject : "",
      body: typeof body === "string" ? body : "",
      // 差出人名はクライアントの申告ではなくログイン中のユーザーから取る
      // (他人の名前で下書きを作らせない)。
      sender_name: user.name ?? "",
      client_page_ids: clientPageIds.filter((id): id is string => typeof id === "string"),
    });
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
