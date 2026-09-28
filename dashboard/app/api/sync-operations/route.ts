import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { BackendApiError, resumeProductHold } from "@/lib/backend";

export const maxDuration = 300;

export async function POST(request: Request) {
  let user;
  try { user = await getCurrentUser(); } catch {
    return NextResponse.json({ detail: "認証状態を確認できません" }, { status: 503 });
  }
  if (!user) return NextResponse.json({ detail: "ログインが必要です" }, { status: 401 });
  if (!user.isManager) return NextResponse.json({ detail: "マネージャーだけが処理できます" }, { status: 403 });
  // 同一サイトの画面操作だけを受け付け、actor_id は必ずセッションから設定する。
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  const host = request.headers.get("host") ?? new URL(request.url).host;
  let sameHost = false;
  try {
    const source = new URL(origin ?? "");
    sameHost = ["http:", "https:"].includes(source.protocol) && source.host === host.toLowerCase();
  } catch { /* 不正な送信元は拒否する。 */ }
  if (!sameHost || site === "cross-site" || site === "same-site") {
    return NextResponse.json({ detail: "画面を読み直してください" }, { status: 403 });
  }
  const body = await request.json().catch(() => null);
  if (!body || typeof body.project_id !== "string" || !/^[a-f0-9]{64}$/.test(body.expected_hash ?? "")) {
    return NextResponse.json({ detail: "操作の形式が不正です" }, { status: 400 });
  }
  try {
    return NextResponse.json(await resumeProductHold({
      project_id: body.project_id, expected_hash: body.expected_hash, actor_id: user.id,
    }));
  } catch (error) {
    return NextResponse.json({ detail: error instanceof BackendApiError ? error.message : "処理に失敗しました" },
      { status: error instanceof BackendApiError ? error.status ?? 500 : 500 });
  }
}
