import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BackendApiError,
  getDashboardSummary,
  getManagerAlerts,
  getRevenueTargetSheetSettings,
  requestQuoteApproval,
  saveRevenueTargetSheetSettings,
} from "@/lib/backend";

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
        quarter: {
          range: { start: "2026-06-01", end: "2026-08-31" },
          max: { initial_fee: 1, mrr: 1 },
          expected: { initial_fee: 1, mrr: 1 },
          min: { initial_fee: 1, mrr: 1 },
        },
        half: {
          range: { start: "2026-06-01", end: "2026-11-30" },
          max: { initial_fee: 1, mrr: 1 },
          expected: { initial_fee: 1, mrr: 1 },
          min: { initial_fee: 1, mrr: 1 },
        },
        year: {
          range: { start: "2025-12-01", end: "2026-11-30" },
          max: { initial_fee: 1, mrr: 1 },
          expected: { initial_fee: 1, mrr: 1 },
          min: { initial_fee: 1, mrr: 1 },
        },
        unscheduled_active_count: 0,
        unscheduled_confirmed_count: 0,
      },
      notes: ["予想契約日が未入力の進行中案件が0件あり、上記の着地予測には含まれていません。"],
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

describe("revenue target sheet settings", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.BACKEND_API_URL = "http://localhost:8000";
    process.env.BACKEND_API_TOKEN = "test-token";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  it("getRevenueTargetSheetSettings: GETでバックエンドの設定をそのまま返す", async () => {
    const body = { configured: false, pointer: null, updated_at: null };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getRevenueTargetSheetSettings();

    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/settings/revenue-target-sheet",
      expect.objectContaining({
        method: "GET",
        headers: { Authorization: "Bearer test-token" },
      })
    );
  });

  it("saveRevenueTargetSheetSettings: POSTでJSONボディを送信する", async () => {
    const responseBody = {
      pointer: { spreadsheet_id: "sheet-abc", mrr_sheet_name: "MRRシート", unit_count_sheet_name: null },
      updated_at: "2026-08-13T09:00:00",
      validation_success: true,
      validation_error: null,
      mrr_month_count: 12,
      unit_count_month_count: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload = {
      spreadsheet_url_or_id: "https://docs.google.com/spreadsheets/d/sheet-abc/edit",
      mrr_sheet_name: "MRRシート",
      unit_count_sheet_name: null,
    };
    const result = await saveRevenueTargetSheetSettings(payload);

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/settings/revenue-target-sheet",
      expect.objectContaining({
        method: "POST",
        headers: {
          Authorization: "Bearer test-token",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      })
    );
  });

  it("saveRevenueTargetSheetSettings: 非2xxレスポンスでBackendApiErrorが投げられる", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid" }), { status: 422 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      saveRevenueTargetSheetSettings({
        spreadsheet_url_or_id: "",
        mrr_sheet_name: null,
        unit_count_sheet_name: null,
      })
    ).rejects.toMatchObject({ status: 422 });
  });

  it("requestQuoteApproval: snake_caseへ変換してPOSTし、レスポンスをそのまま返す", async () => {
    const responseBody = {
      drive_file_id: "file-1",
      drive_approval_id: "approval-1",
      document_approval_id: "row-1",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await requestQuoteApproval({
      projectId: "abc123",
      approverEmails: ["approver@example.com"],
      requestedByEmail: "rep@example.com",
      message: "ご確認お願いします",
    });

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/documents/quote/request-approval",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          project_id: "abc123",
          approver_emails: ["approver@example.com"],
          requested_by_email: "rep@example.com",
          message: "ご確認お願いします",
        }),
      })
    );
  });

  it("requestQuoteApproval: 複数承認者を渡すとapprover_emailsに全件配列で渡る", async () => {
    const responseBody = {
      drive_file_id: "file-1",
      drive_approval_id: "approval-1",
      document_approval_id: "row-1",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await requestQuoteApproval({
      projectId: "abc123",
      approverEmails: ["a@example.com", "b@example.com"],
      requestedByEmail: "rep@example.com",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/documents/quote/request-approval",
      expect.objectContaining({
        body: expect.stringContaining(
          JSON.stringify(["a@example.com", "b@example.com"])
        ),
      })
    );
  });

  it("requestQuoteApproval: 422はBackendApiErrorとしてdetailを保持する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "rep@example.comのDrive連携が未接続です。" }), {
        status: 422,
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      requestQuoteApproval({
        projectId: "abc123",
        approverEmails: ["approver@example.com"],
        requestedByEmail: "rep@example.com",
      })
    ).rejects.toMatchObject({ status: 422, message: "rep@example.comのDrive連携が未接続です。" });
  });
});
