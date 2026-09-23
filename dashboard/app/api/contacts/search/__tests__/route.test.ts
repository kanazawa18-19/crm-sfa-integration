import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const getCurrentUserMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

const { GET } = await import("@/app/api/contacts/search/route");

const ORIGINAL_ENV = { ...process.env };

function makeRequest(query: string): NextRequest {
  return new NextRequest(`http://localhost/api/contacts/search${query}`);
}

describe("GET /api/contacts/search", () => {
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

  it("未ログイン時は401を返す", async () => {
    getCurrentUserMock.mockResolvedValue(null);

    const response = await GET(makeRequest("?q=山田"));

    expect(response.status).toBe(401);
  });

  it("正常系: バックエンドの検索結果(取引先の紐づき込み)をそのまま返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            contacts: [
              {
                notion_page_id: "cnt-1",
                名前: "山田太郎",
                部署: "営業部",
                役職: "課長",
                取引先: [
                  { notion_page_id: "cli-1", 取引先名: "サンプルホテル", 取引先名_status: "resolved" },
                ],
              },
            ],
            truncated: false,
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
      )
    );

    const response = await GET(makeRequest("?q=山田"));

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.contacts[0].取引先).toEqual([
      { notion_page_id: "cli-1", 取引先名: "サンプルホテル", 取引先名_status: "resolved" },
    ]);
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

    const response = await GET(makeRequest("?q=山田"));

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body.detail).toBe("不正なリクエストです");
  });

  it("qパラメータ未指定時は空文字として扱う", async () => {
    const fakeFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ contacts: [], truncated: false }), {
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
