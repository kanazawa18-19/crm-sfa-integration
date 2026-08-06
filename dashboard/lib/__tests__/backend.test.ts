import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackendApiError, getDashboardSummary } from "@/lib/backend";

const ORIGINAL_ENV = { ...process.env };

describe("backend", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://localhost:8000";
    process.env.BACKEND_API_TOKEN = "test-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("正常系: 期待した型のJSONが返る", async () => {
    const body = {
      as_of: "2026-08-06",
      forecast: {
        max: { initial_fee: 1, mrr: 1 },
        expected: { initial_fee: 1, mrr: 1 },
        min: { initial_fee: 1, mrr: 1 },
      },
      status_breakdown: [],
      totals: {
        project_count: 0,
        confirmed_count: 0,
        active_count: 0,
        lost_count: 0,
        cancelled_count: 0,
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await getDashboardSummary();
    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/dashboard/summary",
      expect.objectContaining({
        headers: { Authorization: "Bearer test-token" },
      })
    );
  });

  it("非2xxレスポンスで BackendApiError が投げられ status が含まれる", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("Internal Server Error", { status: 500 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getDashboardSummary()).rejects.toMatchObject({
      status: 500,
    });
    await expect(getDashboardSummary()).rejects.toBeInstanceOf(BackendApiError);
  });

  it("fetch が例外を投げた場合（ネットワークエラー）は BackendApiError に変換される", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("network error"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getDashboardSummary()).rejects.toBeInstanceOf(BackendApiError);
  });

  it("BACKEND_API_URL 未設定時は BackendApiError が投げられる", async () => {
    delete process.env.BACKEND_API_URL;
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(getDashboardSummary()).rejects.toBeInstanceOf(BackendApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
