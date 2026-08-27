import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const getCurrentUserMock = vi.fn();
const requestQuoteApprovalMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

vi.mock("@/lib/backend", async () => {
  const actual = await vi.importActual<typeof import("@/lib/backend")>("@/lib/backend");
  return {
    ...actual,
    requestQuoteApproval: (...args: unknown[]) => requestQuoteApprovalMock(...args),
  };
});

const { POST } = await import("@/app/api/documents/quote/request-approval/route");
const { BackendApiError } = await import("@/lib/backend");

function makeRequest(body: unknown): NextRequest {
  return new NextRequest("http://localhost/api/documents/quote/request-approval", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

describe("POST /api/documents/quote/request-approval", () => {
  beforeEach(() => {
    getCurrentUserMock.mockReset();
    requestQuoteApprovalMock.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("未ログインの場合は401を返す(バックエンドを呼ばない)", async () => {
    getCurrentUserMock.mockResolvedValue(null);

    const response = await POST(
      makeRequest({ project_id: "abc", approver_emails: ["approver@example.com"] })
    );

    expect(response.status).toBe(401);
    expect(requestQuoteApprovalMock).not.toHaveBeenCalled();
  });

  it("project_id・approver_emailsが欠けている場合は400を返す", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });

    const response = await POST(makeRequest({ project_id: "abc" }));

    expect(response.status).toBe(400);
    expect(requestQuoteApprovalMock).not.toHaveBeenCalled();
  });

  it("approver_emailsが空配列の場合は400を返す", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });

    const response = await POST(makeRequest({ project_id: "abc", approver_emails: [] }));

    expect(response.status).toBe(400);
    expect(requestQuoteApprovalMock).not.toHaveBeenCalled();
  });

  it("requested_by_emailはクライアント入力を無視し、セッションのメールを使う", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    requestQuoteApprovalMock.mockResolvedValue({
      drive_file_id: "file-1",
      drive_approval_id: "approval-1",
      document_approval_id: "row-1",
    });

    const response = await POST(
      makeRequest({
        project_id: "abc",
        approver_emails: ["approver@example.com"],
        // 詐称を試みても無視されることを確認する。
        requested_by_email: "someone-else@example.com",
        message: "ご確認お願いします",
      })
    );

    expect(response.status).toBe(200);
    expect(requestQuoteApprovalMock).toHaveBeenCalledWith({
      projectId: "abc",
      approverEmails: ["approver@example.com"],
      requestedByEmail: "rep@example.com",
      message: "ご確認お願いします",
      overrides: {
        memo: undefined,
        clientName: undefined,
        serviceName: undefined,
        initialFee: undefined,
        monthlyFee: undefined,
        creatorName: undefined,
      },
    });
  });

  it("approver_emailsに複数件渡すと全件そのまま中継する", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    requestQuoteApprovalMock.mockResolvedValue({
      drive_file_id: "file-1",
      drive_approval_id: "approval-1",
      document_approval_id: "row-1",
    });

    await POST(
      makeRequest({
        project_id: "abc",
        approver_emails: ["a@example.com", "b@example.com"],
      })
    );

    expect(requestQuoteApprovalMock).toHaveBeenCalledWith(
      expect.objectContaining({ approverEmails: ["a@example.com", "b@example.com"] })
    );
  });

  it("移行期の互換: 旧フォーマットの単数approver_email(文字列)も1要素配列として受理する", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    requestQuoteApprovalMock.mockResolvedValue({
      drive_file_id: "file-1",
      drive_approval_id: "approval-1",
      document_approval_id: "row-1",
    });

    const response = await POST(
      makeRequest({ project_id: "abc", approver_email: "approver@example.com" })
    );

    expect(response.status).toBe(200);
    expect(requestQuoteApprovalMock).toHaveBeenCalledWith(
      expect.objectContaining({ approverEmails: ["approver@example.com"] })
    );
  });

  it("手動入力欄(overrides)をそのまま中継する", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    requestQuoteApprovalMock.mockResolvedValue({
      drive_file_id: "file-1",
      drive_approval_id: "approval-1",
      document_approval_id: "row-1",
    });

    await POST(
      makeRequest({
        project_id: "abc",
        approver_emails: ["approver@example.com"],
        memo: "特記事項です",
        client_name: "テスト商店",
        service_name: "リピッテ",
        initial_fee: "100,000",
        monthly_fee: "30,000",
        creator_name: "金沢",
      })
    );

    expect(requestQuoteApprovalMock).toHaveBeenCalledWith(
      expect.objectContaining({
        overrides: {
          memo: "特記事項です",
          clientName: "テスト商店",
          serviceName: "リピッテ",
          initialFee: "100,000",
          monthlyFee: "30,000",
          creatorName: "金沢",
        },
      })
    );
  });

  it("バックエンドのエラーステータス・メッセージをそのまま中継する", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    requestQuoteApprovalMock.mockRejectedValue(
      new BackendApiError("rep@example.comのDrive連携が未接続です。", 422)
    );

    const response = await POST(
      makeRequest({ project_id: "abc", approver_emails: ["approver@example.com"] })
    );

    expect(response.status).toBe(422);
    const body = await response.json();
    expect(body.detail).toContain("Drive連携");
  });
});
