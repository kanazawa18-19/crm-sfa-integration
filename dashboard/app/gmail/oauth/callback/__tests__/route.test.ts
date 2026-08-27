import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";
const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive";

const getCurrentUserMock = vi.fn();
const exchangeCodeForTokenMock = vi.fn();
const upsertGmailMock = vi.fn();
const upsertDriveMock = vi.fn();
const transactionMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

vi.mock("@/lib/gmailOauth", () => ({
  exchangeCodeForToken: (code: string) => exchangeCodeForTokenMock(code),
  GMAIL_SCOPE,
}));

vi.mock("@/lib/googleOauth", () => ({
  DRIVE_SCOPE,
}));

vi.mock("@/lib/tokenCrypto", () => ({
  encryptToken: (plaintext: string) => `enc:${plaintext}`,
}));

vi.mock("@/lib/prisma", () => ({
  default: {
    repGmailConnection: { upsert: (...args: unknown[]) => upsertGmailMock(...args) },
    repDriveConnection: { upsert: (...args: unknown[]) => upsertDriveMock(...args) },
    $transaction: (...args: unknown[]) => transactionMock(...args),
  },
}));

const { GET } = await import("@/app/gmail/oauth/callback/route");

function makeRequest(searchParams: string, cookieValue?: string): NextRequest {
  const headers: Record<string, string> = {};
  if (cookieValue !== undefined) {
    headers.Cookie = `gmail_oauth_state=${cookieValue}`;
  }
  return new NextRequest(`http://localhost/gmail/oauth/callback?${searchParams}`, { headers });
}

describe("GET /gmail/oauth/callback", () => {
  beforeEach(() => {
    getCurrentUserMock.mockReset();
    exchangeCodeForTokenMock.mockReset();
    upsertGmailMock.mockReset();
    upsertDriveMock.mockReset();
    transactionMock.mockReset();

    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    exchangeCodeForTokenMock.mockResolvedValue({
      refreshToken: "refresh-token-abc",
      grantedScopes: [GMAIL_SCOPE, DRIVE_SCOPE],
    });
    upsertGmailMock.mockResolvedValue({});
    upsertDriveMock.mockResolvedValue({});
    transactionMock.mockResolvedValue([{}, {}]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe("purpose=google_all(統合フロー)", () => {
    it("両方のscopeが許可されたとき、GmailとDriveの両方へ同じ暗号化トークンをtransactionでupsertする", async () => {
      const response = await GET(makeRequest("code=auth-code&state=nonce123.google_all", "nonce123"));

      expect(response.status).toBe(307);
      expect(response.headers.get("location")).toBe("http://localhost/settings/google?connected=1");

      expect(transactionMock).toHaveBeenCalledTimes(1);
      const batch = transactionMock.mock.calls[0][0] as unknown[];
      expect(batch).toHaveLength(2);

      expect(upsertGmailMock).toHaveBeenCalledWith({
        where: { repEmail: "rep@example.com" },
        update: { refreshTokenEnc: "enc:refresh-token-abc" },
        create: { repEmail: "rep@example.com", refreshTokenEnc: "enc:refresh-token-abc" },
      });
      expect(upsertDriveMock).toHaveBeenCalledWith({
        where: { repEmail: "rep@example.com" },
        update: { refreshTokenEnc: "enc:refresh-token-abc" },
        create: { repEmail: "rep@example.com", refreshTokenEnc: "enc:refresh-token-abc" },
      });
    });

    it("Gmailのscopeだけ許可された場合(Driveのチェックを外した)、Gmailのみupsertし、missing=driveで/settings/googleへ返す", async () => {
      exchangeCodeForTokenMock.mockResolvedValue({
        refreshToken: "refresh-token-abc",
        grantedScopes: [GMAIL_SCOPE],
      });

      const response = await GET(makeRequest("code=auth-code&state=nonce123.google_all", "nonce123"));

      expect(response.headers.get("location")).toBe(
        "http://localhost/settings/google?connected=1&missing=drive"
      );
      expect(transactionMock).not.toHaveBeenCalled();
      expect(upsertGmailMock).toHaveBeenCalledWith({
        where: { repEmail: "rep@example.com" },
        update: { refreshTokenEnc: "enc:refresh-token-abc" },
        create: { repEmail: "rep@example.com", refreshTokenEnc: "enc:refresh-token-abc" },
      });
      expect(upsertDriveMock).not.toHaveBeenCalled();
    });

    it("Driveのscopeだけ許可された場合(Gmailのチェックを外した)、Driveのみupsertし、missing=gmailで/settings/googleへ返す", async () => {
      exchangeCodeForTokenMock.mockResolvedValue({
        refreshToken: "refresh-token-abc",
        grantedScopes: [DRIVE_SCOPE],
      });

      const response = await GET(makeRequest("code=auth-code&state=nonce123.google_all", "nonce123"));

      expect(response.headers.get("location")).toBe(
        "http://localhost/settings/google?connected=1&missing=gmail"
      );
      expect(transactionMock).not.toHaveBeenCalled();
      expect(upsertDriveMock).toHaveBeenCalledWith({
        where: { repEmail: "rep@example.com" },
        update: { refreshTokenEnc: "enc:refresh-token-abc" },
        create: { repEmail: "rep@example.com", refreshTokenEnc: "enc:refresh-token-abc" },
      });
      expect(upsertGmailMock).not.toHaveBeenCalled();
    });

    it("どちらのscopeも許可されなかった場合、何もupsertせずscope_deniedで失敗する", async () => {
      exchangeCodeForTokenMock.mockResolvedValue({ refreshToken: "refresh-token-abc", grantedScopes: [] });

      const response = await GET(makeRequest("code=auth-code&state=nonce123.google_all", "nonce123"));

      expect(response.headers.get("location")).toBe("http://localhost/settings/google?error=scope_denied");
      expect(transactionMock).not.toHaveBeenCalled();
      expect(upsertGmailMock).not.toHaveBeenCalled();
      expect(upsertDriveMock).not.toHaveBeenCalled();
    });
  });

  describe("purposeなし(Gmail単体フロー)", () => {
    it("従来通りGmailのscopeが許可されたときはGmailのみupsertし、/settings/gmailへ返す(既存挙動の回帰確認)", async () => {
      exchangeCodeForTokenMock.mockResolvedValue({
        refreshToken: "refresh-token-abc",
        grantedScopes: [GMAIL_SCOPE],
      });

      const response = await GET(makeRequest("code=auth-code&state=nonce123", "nonce123"));

      expect(response.status).toBe(307);
      expect(response.headers.get("location")).toBe("http://localhost/settings/gmail?connected=1");

      expect(upsertGmailMock).toHaveBeenCalledWith({
        where: { repEmail: "rep@example.com" },
        update: { refreshTokenEnc: "enc:refresh-token-abc" },
        create: { repEmail: "rep@example.com", refreshTokenEnc: "enc:refresh-token-abc" },
      });
      expect(upsertDriveMock).not.toHaveBeenCalled();
      expect(transactionMock).not.toHaveBeenCalled();
    });

    it("Gmailのscopeが許可されなかった場合(同意画面でチェックを外した)、upsertせずscope_deniedで失敗する", async () => {
      exchangeCodeForTokenMock.mockResolvedValue({ refreshToken: "refresh-token-abc", grantedScopes: [] });

      const response = await GET(makeRequest("code=auth-code&state=nonce123", "nonce123"));

      expect(response.headers.get("location")).toBe("http://localhost/settings/gmail?error=scope_denied");
      expect(upsertGmailMock).not.toHaveBeenCalled();
    });
  });

  it("nonceがcookieと一致しない場合はinvalid_stateで失敗する", async () => {
    const response = await GET(makeRequest("code=auth-code&state=nonce123.google_all", "different-nonce"));

    expect(response.headers.get("location")).toBe(
      "http://localhost/settings/google?error=invalid_state"
    );
    expect(exchangeCodeForTokenMock).not.toHaveBeenCalled();
  });

  it("失敗レスポンスでも使い捨てのnonce cookieを削除する(invalid_state)", async () => {
    const response = await GET(makeRequest("code=auth-code&state=nonce123.google_all", "different-nonce"));

    expect(response.cookies.get("gmail_oauth_state")?.value).toBe("");
  });

  it("失敗レスポンスでも使い捨てのnonce cookieを削除する(scope_denied)", async () => {
    exchangeCodeForTokenMock.mockResolvedValue({ refreshToken: "refresh-token-abc", grantedScopes: [] });

    const response = await GET(makeRequest("code=auth-code&state=nonce123", "nonce123"));

    expect(response.cookies.get("gmail_oauth_state")?.value).toBe("");
  });

  it("未知のpurposeはinvalid_stateとして失敗し、Gmailのエラーページへ返す", async () => {
    const response = await GET(makeRequest("code=auth-code&state=nonce123.unknown_purpose", "nonce123"));

    expect(response.headers.get("location")).toBe(
      "http://localhost/settings/gmail?error=invalid_state"
    );
    expect(exchangeCodeForTokenMock).not.toHaveBeenCalled();
  });

  it("codeが無い場合はinvalid_stateで失敗する", async () => {
    const response = await GET(makeRequest("state=nonce123", "nonce123"));

    expect(response.headers.get("location")).toBe(
      "http://localhost/settings/gmail?error=invalid_state"
    );
  });

  it("トークン交換が例外を投げた場合はexchange_failedで失敗する", async () => {
    exchangeCodeForTokenMock.mockRejectedValue(new Error("boom"));

    const response = await GET(makeRequest("code=auth-code&state=nonce123", "nonce123"));

    expect(response.headers.get("location")).toBe(
      "http://localhost/settings/gmail?error=exchange_failed"
    );
  });
});
