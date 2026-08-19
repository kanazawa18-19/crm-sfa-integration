import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { GET } from "@/app/api/documents/generate/route";

const ORIGINAL_ENV = { ...process.env };

function makeRequest(query: string): NextRequest {
  return new NextRequest(`http://localhost/api/documents/generate${query}`);
}

describe("GET /api/documents/generate", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://backend.example.com";
    process.env.BACKEND_API_TOKEN = "secret-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("必須パラメータが欠けている場合は400を返す", async () => {
    const response = await GET(makeRequest("?notion_project_id=abc"));
    expect(response.status).toBe(400);
  });

  it("BACKEND_API_URL未設定時は500を返す", async () => {
    delete process.env.BACKEND_API_URL;
    const response = await GET(makeRequest("?notion_project_id=abc&category=見積書"));
    expect(response.status).toBe(500);
  });

  it("バックエンドのContent-Type・Content-Disposition・X-Document-Notesを中継する", async () => {
    const fakeFetch = vi.fn().mockResolvedValue(
      new Response(new Uint8Array([1, 2, 3]).buffer, {
        status: 200,
        headers: {
          "content-type": "application/pdf",
          "content-disposition": "attachment; filename*=UTF-8''%E8%A6%8B%E7%A9%8D%E6%9B%B8.pdf",
          "x-document-notes": "%5B%22note1%22%5D",
        },
      })
    );
    vi.stubGlobal("fetch", fakeFetch);

    const response = await GET(makeRequest("?notion_project_id=abc&category=見積書"));

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/pdf");
    expect(response.headers.get("content-disposition")).toContain("filename*=UTF-8''");
    expect(response.headers.get("x-document-notes")).toBe("%5B%22note1%22%5D");
    expect(fakeFetch).toHaveBeenCalledWith(
      expect.stringContaining("http://backend.example.com/api/documents/generate?"),
      expect.objectContaining({
        headers: { Authorization: "Bearer secret-token" },
      })
    );
  });

  it("バックエンドのエラーレスポンス（422等）のステータス・本文をそのまま中継する", async () => {
    const fakeFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "テンプレート未対応です" }), {
        status: 422,
        headers: { "content-type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fakeFetch);

    const response = await GET(makeRequest("?notion_project_id=abc&category=見積書"));

    expect(response.status).toBe(422);
    const body = await response.json();
    expect(body.detail).toBe("テンプレート未対応です");
  });

  it("手動入力欄(overrides)は値が入っているものだけクエリへ中継する", async () => {
    const fakeFetch = vi.fn().mockResolvedValue(
      new Response(new Uint8Array([1, 2, 3]).buffer, {
        status: 200,
        headers: { "content-type": "application/pdf" },
      })
    );
    vi.stubGlobal("fetch", fakeFetch);

    await GET(
      makeRequest(
        "?notion_project_id=abc&category=見積書&memo=特記事項&creator_name=金沢&client_name="
      )
    );

    const calledUrl = fakeFetch.mock.calls[0][0] as string;
    expect(calledUrl).toContain("memo=%E7%89%B9%E8%A8%98%E4%BA%8B%E9%A0%85");
    expect(calledUrl).toContain("creator_name=%E9%87%91%E6%B2%A2");
    expect(calledUrl).not.toContain("client_name=");
  });

  it("バックエンドへの接続自体に失敗した場合は502を返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("network error"))
    );

    const response = await GET(makeRequest("?notion_project_id=abc&category=見積書"));

    expect(response.status).toBe(502);
  });
});
