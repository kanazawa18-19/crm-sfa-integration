import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// バックエンドの `GET /api/documents/generate` をそのまま中継する。
// BACKEND_API_URL / BACKEND_API_TOKEN の参照方法は lib/backend.ts の fetchBackend に揃える。
export async function GET(request: NextRequest) {
  const notionProjectId = request.nextUrl.searchParams.get("notion_project_id");
  const category = request.nextUrl.searchParams.get("category");

  if (!notionProjectId || !category) {
    return NextResponse.json(
      { detail: "notion_project_id と category は必須です" },
      { status: 400 }
    );
  }

  const baseUrl = process.env.BACKEND_API_URL;
  if (!baseUrl) {
    return NextResponse.json({ detail: "BACKEND_API_URL が設定されていません" }, { status: 500 });
  }
  const token = process.env.BACKEND_API_TOKEN;

  const query = new URLSearchParams({
    notion_project_id: notionProjectId,
    category,
  });

  let backendResponse: Response;
  try {
    backendResponse = await fetch(`${baseUrl}/api/documents/generate?${query.toString()}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      cache: "no-store",
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: `バックエンドAPIへの接続に失敗しました: ${
          error instanceof Error ? error.message : String(error)
        }`,
      },
      { status: 502 }
    );
  }

  const headers = new Headers();
  const contentType = backendResponse.headers.get("content-type");
  const contentDisposition = backendResponse.headers.get("content-disposition");
  const documentNotes = backendResponse.headers.get("x-document-notes");
  if (contentType) {
    headers.set("Content-Type", contentType);
  }
  if (contentDisposition) {
    headers.set("Content-Disposition", contentDisposition);
  }
  if (documentNotes) {
    headers.set("X-Document-Notes", documentNotes);
  }

  const body = await backendResponse.arrayBuffer();
  return new NextResponse(body, { status: backendResponse.status, headers });
}
