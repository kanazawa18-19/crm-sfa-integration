import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

/**
 * 「Googleでログイン」の検証（2026-08-31）。
 *
 * このフローは**ログイン前に走る**うえ、連携フローと同じコールバックURLを共用している。
 * 事故ると「誰でも入れる」か「誰も入れない」のどちらかになるので、
 * 守るべき性質を明示的に固定する。
 */

const getCurrentUserMock = vi.fn();
const exchangeIdentityMock = vi.fn();
const findUserMock = vi.fn();
const establishSessionMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

vi.mock("@/lib/gmailOauth", () => ({
  exchangeCodeForToken: vi.fn(),
  GMAIL_SCOPE: "https://www.googleapis.com/auth/gmail.readonly",
}));

vi.mock("@/lib/googleOauth", () => ({
  DRIVE_SCOPE: "https://www.googleapis.com/auth/drive",
}));

vi.mock("@/lib/tokenCrypto", () => ({ encryptToken: (s: string) => s }));

vi.mock("@/lib/googleLoginOauth", () => ({
  exchangeCodeForGoogleIdentity: (code: string) => exchangeIdentityMock(code),
}));

vi.mock("@/app/actions", () => ({
  establishSessionForUser: (userId: string) => establishSessionMock(userId),
}));

vi.mock("@/lib/prisma", () => ({
  default: {
    user: { findUnique: (...args: unknown[]) => findUserMock(...args) },
    repGmailConnection: { upsert: vi.fn() },
    repDriveConnection: { upsert: vi.fn() },
    $transaction: vi.fn(),
  },
}));

const { GET } = await import("@/app/gmail/oauth/callback/route");

const NONCE = "nonce-abc";

function makeRequest({
  state = `${NONCE}.admin_login`,
  code = "auth-code",
  cookie = NONCE,
}: { state?: string | null; code?: string | null; cookie?: string | null } = {}): NextRequest {
  const params = new URLSearchParams();
  if (code !== null) params.set("code", code);
  if (state !== null) params.set("state", state);
  const headers: Record<string, string> = {};
  if (cookie !== null) headers.Cookie = `admin_login_oauth_state=${cookie}`;
  return new NextRequest(`http://localhost/gmail/oauth/callback?${params}`, { headers });
}

function errorOf(response: Response): string | null {
  return new URL(response.headers.get("location") ?? "", "http://localhost").searchParams.get("error");
}

describe("Googleでログイン（/gmail/oauth/callback の admin_login 分岐）", () => {
  beforeEach(() => {
    getCurrentUserMock.mockReset();
    exchangeIdentityMock.mockReset();
    findUserMock.mockReset();
    establishSessionMock.mockReset();

    // ログイン前なのでセッションは無い。それが既定の状態。
    getCurrentUserMock.mockResolvedValue(null);
    exchangeIdentityMock.mockResolvedValue({ email: "admin@example.com", verifiedEmail: true });
    findUserMock.mockResolvedValue({ id: "user-1", email: "admin@example.com" });
    establishSessionMock.mockResolvedValue({ needsTwoFactor: false, redirectTo: "/" });
  });

  it("セッションが無くてもログインできる（連携フローの認証チェックに巻き込まれない）", async () => {
    const response = await GET(makeRequest());

    expect(getCurrentUserMock).not.toHaveBeenCalled();
    expect(response.headers.get("location")).toBe("http://localhost/");
  });

  it("2FAが有効ならGoogleログインでも2FA画面へ送る（迂回させない）", async () => {
    establishSessionMock.mockResolvedValue({ needsTwoFactor: true, redirectTo: "/login/2fa" });

    const response = await GET(makeRequest());

    expect(response.headers.get("location")).toBe("http://localhost/login/2fa");
  });

  it("該当する管理者アカウントが無ければ拒否する（自動作成しない）", async () => {
    findUserMock.mockResolvedValue(null);

    const response = await GET(makeRequest());

    expect(establishSessionMock).not.toHaveBeenCalled();
    expect(response.headers.get("location")).toContain("/login");
    expect(errorOf(response)).toContain("見つかりません");
  });

  it("Google側でメールが未確認なら拒否する", async () => {
    exchangeIdentityMock.mockResolvedValue({ email: "admin@example.com", verifiedEmail: false });

    const response = await GET(makeRequest());

    expect(findUserMock).not.toHaveBeenCalled();
    expect(establishSessionMock).not.toHaveBeenCalled();
    expect(errorOf(response)).toContain("確認済みではありません");
  });

  it("nonceが一致しなければ拒否する（CSRF対策が効いていること）", async () => {
    const response = await GET(makeRequest({ cookie: "different-nonce" }));

    expect(exchangeIdentityMock).not.toHaveBeenCalled();
    expect(errorOf(response)).toContain("検証に失敗");
  });

  it("nonceのcookieが無ければ拒否する", async () => {
    const response = await GET(makeRequest({ cookie: null }));

    expect(exchangeIdentityMock).not.toHaveBeenCalled();
    expect(errorOf(response)).toContain("検証に失敗");
  });

  it("トークン交換が失敗しても、内部エラーを画面に出さない", async () => {
    exchangeIdentityMock.mockRejectedValue(new Error("Google token exchange failed: 400"));

    const response = await GET(makeRequest());

    expect(errorOf(response)).toBe("Googleログインに失敗しました");
  });

  it("失敗したときもnonceのcookieを消す（使い回しを防ぐ）", async () => {
    findUserMock.mockResolvedValue(null);

    const response = await GET(makeRequest());

    expect(response.headers.get("set-cookie") ?? "").toContain("admin_login_oauth_state=;");
  });

  it("成功したときもnonceのcookieを消す", async () => {
    const response = await GET(makeRequest());

    expect(response.headers.get("set-cookie") ?? "").toContain("admin_login_oauth_state=;");
  });

  it("purposeがadmin_loginでなければ、この分岐に入らない（連携フローのまま扱う）", async () => {
    const response = await GET(makeRequest({ state: `${NONCE}.google_all` }));

    // セッションが無いので連携フロー側の認証チェックで /login へ落ちる。
    expect(getCurrentUserMock).toHaveBeenCalled();
    expect(exchangeIdentityMock).not.toHaveBeenCalled();
    expect(response.headers.get("location")).toBe("http://localhost/login");
  });
});
