import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { BackendApiError, emailReviewRequest } from "@/lib/backend";

async function handle(request: Request) {
  try {
    const user = await getCurrentUser();
    if (!user) return NextResponse.json({ detail: "ログインが必要です" }, { status: 401 });
    if (request.method === "POST") {
      const site = request.headers.get("sec-fetch-site");
      let sameOrigin = false;
      try {
        const origin = new URL(request.headers.get("origin") ?? "");
        const host = request.headers.get("host") ?? new URL(request.url).host;
        sameOrigin = ["http:", "https:"].includes(origin.protocol) && origin.host === host.toLowerCase();
      } catch { /* 不正な送信元は拒否する。 */ }
      if (!sameOrigin || site === "cross-site" || site === "same-site") {
        return NextResponse.json({ detail: "画面を読み直してください" }, { status: 403 });
      }
      const body = await request.json().catch(() => null);
      if (!body || typeof body !== "object" || Array.isArray(body)) return NextResponse.json({ detail: "入力が不正です" }, { status: 400 });
      return NextResponse.json(await emailReviewRequest("/decision", { ...body, actor_id: user.id }));
    }
    const source = new URL(request.url).searchParams;
    const query = new URLSearchParams({ actor_id: user.id });
    if (source.has("kind")) {
      for (const key of ["id", "kind", "query"]) query.set(key, source.get(key) ?? "");
      return NextResponse.json(await emailReviewRequest(`/options?${query}`));
    }
    query.set("state", source.get("state") ?? "pending");
    query.set("offset", source.get("offset") ?? "0");
    return NextResponse.json(await emailReviewRequest(`?${query}`));
  } catch (error) {
    return NextResponse.json({ detail: error instanceof BackendApiError ? error.message : "読込みに失敗しました" },
      { status: error instanceof BackendApiError ? error.status ?? 500 : 503 });
  }
}
export const GET = handle;
export const POST = handle;
