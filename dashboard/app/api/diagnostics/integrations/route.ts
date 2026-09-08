import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { isIntegrationTarget } from "@/lib/integrationDiagnostics";
import { runSafeDiagnostic } from "@/lib/integrationDiagnosticsServer";

export const maxDuration = 60;
const headers = { "Cache-Control": "no-store" };
export async function POST(request: Request) {
  let user;
  try { user = await getCurrentUser(); } catch {
    return NextResponse.json({ detail: "認証状態を確認できませんでした" }, { status: 503, headers });
  }
  if (!user) return NextResponse.json({ detail: "ログインが必要です" }, { status: 401, headers });
  if (user.role !== "master") return NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403, headers });
  // 他サイトからの手動診断の起動を拒否する。
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  if (!origin || site === "cross-site" || site === "same-site") {
    return NextResponse.json({ detail: "不正な送信元です" }, { status: 403, headers });
  }
  // Nextが内部URLへ再構築する場合も、ブラウザが送った公開Hostで照合する。
  // Hostはブラウザから任意に書き換えられない。転送Hostは参照しない。
  const host = request.headers.get("host") ?? new URL(request.url).host;
  let sameHost = false;
  try {
    const source = new URL(origin);
    sameHost = ["http:", "https:"].includes(source.protocol) && source.host === host.toLowerCase();
  } catch { /* 不正なOriginは拒否する。 */ }
  if (!sameHost) return NextResponse.json({ detail: "不正な送信元です" }, { status: 403, headers });
  const payload = await request.json().catch(() => null);
  if (!isIntegrationTarget(payload?.target)) return NextResponse.json({ detail: "診断対象が不正です" }, { status: 400, headers });
  return NextResponse.json(await runSafeDiagnostic(payload.target), { headers });
}
