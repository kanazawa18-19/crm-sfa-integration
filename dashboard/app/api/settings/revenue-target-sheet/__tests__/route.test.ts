import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { GET, POST } from "@/app/api/settings/revenue-target-sheet/route";

const ORIGINAL_ENV = { ...process.env };

function makeGetRequest(): NextRequest {
  return new NextRequest("http://localhost/api/settings/revenue-target-sheet");
}

function makePostRequest(body: unknown): NextRequest {
  return new NextRequest("http://localhost/api/settings/revenue-target-sheet", {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
  });
}

describe("GET /api/settings/revenue-target-sheet", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://backend.example.com";
    process.env.BACKEND_API_TOKEN = "secret-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("正常系: バックエンドの設定結果をそのまま返す", async () => {
    const body = { configured: false, pointer: null, updated_at: null };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
      )
    );

    const response = await GET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(body);
  });

  it("バックエンドのエラー時、detailメッセージとステータスコードをそのまま伝える", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "notion api error" }), {
          status: 502,
          headers: { "content-type": "application/json" },
        })
      )
    );

    const response = await GET();

    expect(response.status).toBe(502);
    expect((await response.json()).detail).toBe("notion api error");
  });
});

describe("POST /api/settings/revenue-target-sheet", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://backend.example.com";
    process.env.BACKEND_API_TOKEN = "secret-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("spreadsheet_url_or_id が無い場合は400を返す", async () => {
    const response = await POST(makePostRequest({ mrr_sheet_name: "MRRシート" }));

    expect(response.status).toBe(400);
  });

  it("不正なJSONボディの場合は400を返す", async () => {
    const request = new NextRequest("http://localhost/api/settings/revenue-target-sheet", {
      method: "POST",
      body: "not-json",
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);

    expect(response.status).toBe(400);
  });

  it("正常系: バックエンドの検証結果をそのまま返す", async () => {
    const responseBody = {
      pointer: { spreadsheet_id: "sheet-abc", mrr_sheet_name: "MRRシート", unit_count_sheet_name: null },
      updated_at: "2026-08-13T09:00:00",
      validation_success: true,
      validation_error: null,
      mrr_month_count: 12,
      unit_count_month_count: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), { status: 200, headers: { "content-type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      makePostRequest({
        spreadsheet_url_or_id: "https://docs.google.com/spreadsheets/d/sheet-abc/edit",
        mrr_sheet_name: "MRRシート",
      })
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(responseBody);
    const sentBody = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(sentBody).toEqual({
      spreadsheet_url_or_id: "https://docs.google.com/spreadsheets/d/sheet-abc/edit",
      mrr_sheet_name: "MRRシート",
      unit_count_sheet_name: null,
    });
  });

  it("バックエンドのバリデーションエラー（422）をそのまま伝える", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "spreadsheet_url_or_id is empty" }), {
          status: 422,
          headers: { "content-type": "application/json" },
        })
      )
    );

    const response = await POST(makePostRequest({ spreadsheet_url_or_id: "   " }));

    expect(response.status).toBe(422);
    expect((await response.json()).detail).toBe("spreadsheet_url_or_id is empty");
  });
});
