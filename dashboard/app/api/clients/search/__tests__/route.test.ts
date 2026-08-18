import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const getCurrentUserMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

const { GET } = await import("@/app/api/clients/search/route");

const ORIGINAL_ENV = { ...process.env };

function makeRequest(query: string): NextRequest {
  return new NextRequest(`http://localhost/api/clients/search${query}`);
}

describe("GET /api/clients/search", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://backend.example.com";
    process.env.BACKEND_API_TOKEN = "secret-token";
    getCurrentUserMock.mockReset();
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com", role: "viewer" });
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("未ログイン時は401を返す(shirokuma-secレビューWARN対応、2026-08-18)", async () => {
    getCurrentUserMock.mockResolvedValue(null);

    const response = await GET(makeRequest("?q=サンプル"));

    expect(response.status).toBe(401);
  });

  it("正常系: バックエンドの検索結果をそのまま返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            clients: [{ notion_page_id: "c1", 取引先名: "サンプルホテル" }],
            truncated: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
      )
    );

    const response = await GET(makeRequest("?q=サンプル"));

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.truncated).toBe(true);
  });

  it("バックエンドのエラー時、detailメッセージとステータスコードをそのまま伝える", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "不正なリクエストです" }), {
          status: 400,
          headers: { "content-type": "application/json" },
        })
      )
    );

    const response = await GET(makeRequest("?q=サンプル"));

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body.detail).toBe("不正なリクエストです");
  });

  it("qパラメータ未指定時は空文字として扱う", async () => {
    const fakeFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ clients: [], truncated: false }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fakeFetch);

    await GET(makeRequest(""));

    expect(fakeFetch).toHaveBeenCalledWith(
      expect.stringContaining("q="),
      expect.anything()
    );
  });
});
