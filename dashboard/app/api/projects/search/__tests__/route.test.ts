import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { GET } from "@/app/api/projects/search/route";

const ORIGINAL_ENV = { ...process.env };

function makeRequest(query: string): NextRequest {
  return new NextRequest(`http://localhost/api/projects/search${query}`);
}

describe("GET /api/projects/search", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://backend.example.com";
    process.env.BACKEND_API_TOKEN = "secret-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("正常系: バックエンドの検索結果をそのまま返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            projects: [{ notion_page_id: "p1", project_name: "サンプル", status: "アポ", proposed_services: [] }],
            total_matched: 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
      )
    );

    const response = await GET(makeRequest("?q=サンプル"));

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.total_matched).toBe(1);
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
      new Response(JSON.stringify({ projects: [], total_matched: 0 }), {
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
