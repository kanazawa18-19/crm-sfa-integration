import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { BackendApiError, getErrorMessage, requestQuoteApproval } from "@/lib/backend";

// documents/page.tsx（クライアントコンポーネント）からの見積書 承認リクエスト送信を
// バックエンド(FastAPI) /api/documents/quote/request-approval へ中継する(2026-08-18)。
//
// requested_by_email はクライアントから受け取らずサーバー側セッションの値で必ず上書きする
// (他の営業担当のDrive接続を勝手に使ってリクエストを送信できてしまう詐称を防ぐ。
// app/gmail/oauth/callback/route.tsのrepEmail扱いと同じ方針)。
export async function POST(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) {
    return NextResponse.json({ detail: "ログインが必要です" }, { status: 401 });
  }

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ detail: "リクエストボディがJSONとして不正です" }, { status: 400 });
  }

  if (typeof payload !== "object" || payload === null) {
    return NextResponse.json(
      { detail: "project_id と approver_emails は必須です" },
      { status: 400 }
    );
  }

  const rawPayload = payload as {
    project_id?: unknown;
    approver_emails?: unknown;
    // 移行期の互換: デプロイ直後に古いJSを掴んだままのブラウザから単数の
    // approver_email(文字列)が送られてきても400にしない(2026-08-27)。
    approver_email?: unknown;
    message?: string;
    memo?: string;
    client_name?: string;
    service_name?: string;
    initial_fee?: string;
    monthly_fee?: string;
    creator_name?: string;
  };

  const rawApproverEmails: unknown[] | null = Array.isArray(rawPayload.approver_emails)
    ? rawPayload.approver_emails
    : typeof rawPayload.approver_email === "string"
      ? [rawPayload.approver_email]
      : null;

  if (
    typeof rawPayload.project_id !== "string" ||
    rawApproverEmails === null ||
    rawApproverEmails.length === 0 ||
    !rawApproverEmails.every((email): email is string => typeof email === "string")
  ) {
    return NextResponse.json(
      { detail: "project_id と approver_emails は必須です" },
      { status: 400 }
    );
  }

  const approverEmails: string[] = rawApproverEmails as string[];

  const body = rawPayload as {
    project_id: string;
    message?: string;
    memo?: string;
    client_name?: string;
    service_name?: string;
    initial_fee?: string;
    monthly_fee?: string;
    creator_name?: string;
  };

  try {
    const result = await requestQuoteApproval({
      projectId: body.project_id,
      approverEmails,
      requestedByEmail: user.email,
      message: body.message ?? "",
      overrides: {
        memo: body.memo,
        clientName: body.client_name,
        serviceName: body.service_name,
        initialFee: body.initial_fee,
        monthlyFee: body.monthly_fee,
        creatorName: body.creator_name,
      },
    });
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
