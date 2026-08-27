import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const getCurrentUserMock = vi.fn();
const exchangeCodeForRefreshTokenMock = vi.fn();
const upsertDriveMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

vi.mock("@/lib/driveOauth", () => ({
  exchangeCodeForRefreshToken: (code: string) => exchangeCodeForRefreshTokenMock(code),
}));

vi.mock("@/lib/tokenCrypto", () => ({
  encryptToken: (plaintext: string) => `enc:${plaintext}`,
}));

vi.mock("@/lib/prisma", () => ({
  default: {
    repDriveConnection: { upsert: (...args: unknown[]) => upsertDriveMock(...args) },
  },
}));

const { GET } = await import("@/app/drive/oauth/callback/route");

function makeRequest(searchParams: string, cookieValue?: string): NextRequest {
  const headers: Record<string, string> = {};
  if (cookieValue !== undefined) {
    headers.Cookie = `drive_oauth_state=${cookieValue}`;
  }
  return new NextRequest(`http://localhost/drive/oauth/callback?${searchParams}`, { headers });
}

describe("GET /drive/oauth/callback", () => {
  beforeEach(() => {
    getCurrentUserMock.mockReset();
    exchangeCodeForRefreshTokenMock.mockReset();
    upsertDriveMock.mockReset();

    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });
    exchangeCodeForRefreshTokenMock.mockResolvedValue("refresh-token-abc");
    upsertDriveMock.mockResolvedValue({});
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("正常系はrepDriveConnectionをupsertし、/settings/drive?connected=1へ返してstate cookieを削除する", async () => {
    const response = await GET(makeRequest("code=auth-code&state=nonce123", "nonce123"));

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("http://localhost/settings/drive?connected=1");
    expect(upsertDriveMock).toHaveBeenCalledWith({
      where: { repEmail: "rep@example.com" },
      update: { refreshTokenEnc: "enc:refresh-token-abc" },
      create: { repEmail: "rep@example.com", refreshTokenEnc: "enc:refresh-token-abc" },
    });
    expect(response.cookies.get("drive_oauth_state")?.value).toBe("");
  });

  it("nonceがcookieと一致しない場合はinvalid_stateで失敗し、state cookieを削除する", async () => {
    const response = await GET(makeRequest("code=auth-code&state=nonce123", "different-nonce"));

    expect(response.headers.get("location")).toBe("http://localhost/settings/drive?error=invalid_state");
    expect(exchangeCodeForRefreshTokenMock).not.toHaveBeenCalled();
    expect(response.cookies.get("drive_oauth_state")?.value).toBe("");
  });

  it("codeが無い場合はinvalid_stateで失敗し、state cookieを削除する", async () => {
    const response = await GET(makeRequest("state=nonce123", "nonce123"));

    expect(response.headers.get("location")).toBe("http://localhost/settings/drive?error=invalid_state");
    expect(response.cookies.get("drive_oauth_state")?.value).toBe("");
  });

  it("トークン交換が例外を投げた場合はexchange_failedで失敗し、state cookieを削除する", async () => {
    exchangeCodeForRefreshTokenMock.mockRejectedValue(new Error("boom"));

    const response = await GET(makeRequest("code=auth-code&state=nonce123", "nonce123"));

    expect(response.headers.get("location")).toBe("http://localhost/settings/drive?error=exchange_failed");
    expect(upsertDriveMock).not.toHaveBeenCalled();
    expect(response.cookies.get("drive_oauth_state")?.value).toBe("");
  });
});
