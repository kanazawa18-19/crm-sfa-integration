import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackendApiError, getDashboardSummary, getManagerAlerts } from "@/lib/backend";

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

describe("getManagerAlerts", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://localhost:8000";
    process.env.BACKEND_API_TOKEN = "test-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  const body = {
    as_of: "2026-08-12",
    alerts: {
      lost: [],
      lost_candidate: [],
      stalled: [],
      won: [],
    },
    counts: { lost: 0, lost_candidate: 0, stalled: 0, won: 0 },
    stalled_days_threshold: 14,
    notes: ["注記テキスト"],
  };

  it("as_of未指定時はクエリパラメータなしでリクエストする", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await getManagerAlerts();
    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/alerts/manager",
      expect.objectContaining({
        headers: { Authorization: "Bearer test-token" },
      })
    );
  });

  it("as_of指定時はクエリパラメータ付きでリクエストする", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await getManagerAlerts("2026-08-01");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/alerts/manager?as_of=2026-08-01",
      expect.objectContaining({
        headers: { Authorization: "Bearer test-token" },
      })
    );
  });

  it("非2xxレスポンスで BackendApiError が投げられる", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("Internal Server Error", { status: 500 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getManagerAlerts()).rejects.toBeInstanceOf(BackendApiError);
  });
});
