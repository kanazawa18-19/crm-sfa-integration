import { beforeEach, describe, expect, it, vi } from "vitest";

// 送信根拠の一覧(読み取り)の検証(2026-09-03)。
//
// 取引先の連絡先が氏名・メールアドレスごと返るため、権限が抜けると
// ログインしていない相手に連絡先一覧が出る。そこを固定する。

const getCurrentUser = vi.fn();
const consentOverview = vi.fn();

vi.mock("@/lib/auth", () => ({ getCurrentUser: () => getCurrentUser() }));
vi.mock("@/lib/backend", () => ({
  bulkEmailConsentOverview: (...args: unknown[]) => consentOverview(...args),
  BackendApiError: class extends Error {
    status?: number;
  },
  getErrorMessage: (error: unknown) => (error instanceof Error ? error.message : "不明"),
}));

const { POST } = await import("@/app/api/bulk-email/consent-overview/route");

function request(payload: unknown) {
  return { json: async () => payload } as never;
}

beforeEach(() => {
  vi.clearAllMocks();
  getCurrentUser.mockResolvedValue({ id: "u1", email: "a@example.com", role: "editor" });
  consentOverview.mockResolvedValue({ contacts: [], warnings: [], basis_options: [], counts: {} });
});

describe("送信根拠の一覧", () => {
  it("編集者なら取れる", async () => {
    const response = await POST(request({ client_page_ids: ["cli-1"] }));
    expect(response.status).toBe(200);
    expect(consentOverview).toHaveBeenCalledWith({ client_page_ids: ["cli-1"] });
  });

  it("文字列でない取引先IDは落とす", async () => {
    await POST(request({ client_page_ids: ["cli-1", 42, null] }));
    expect(consentOverview).toHaveBeenCalledWith({ client_page_ids: ["cli-1"] });
  });

  it("閲覧者は403", async () => {
    getCurrentUser.mockResolvedValue({ id: "u2", email: "v@example.com", role: "viewer" });
    expect((await POST(request({ client_page_ids: ["cli-1"] }))).status).toBe(403);
    expect(consentOverview).not.toHaveBeenCalled();
  });

  it("ログインしていなければ401", async () => {
    getCurrentUser.mockResolvedValue(null);
    expect((await POST(request({ client_page_ids: [] }))).status).toBe(401);
  });

  it("取引先の指定が配列でなければ400", async () => {
    expect((await POST(request({}))).status).toBe(400);
    expect(consentOverview).not.toHaveBeenCalled();
  });
});
