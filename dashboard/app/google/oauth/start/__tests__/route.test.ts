import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const getCurrentUserMock = vi.fn();
const buildAuthUrlMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => getCurrentUserMock(),
}));

vi.mock("@/lib/googleOauth", () => ({
  buildAuthUrl: (state: string) => buildAuthUrlMock(state),
}));

const { GET } = await import("@/app/google/oauth/start/route");

function makeRequest(): NextRequest {
  return new NextRequest("http://localhost/google/oauth/start");
}

describe("GET /google/oauth/start", () => {
  beforeEach(() => {
    getCurrentUserMock.mockReset();
    buildAuthUrlMock.mockReset();
    buildAuthUrlMock.mockReturnValue("https://accounts.google.com/o/oauth2/v2/auth?mocked=1");
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("未ログインの場合はログイン画面へリダイレクトする", async () => {
    getCurrentUserMock.mockResolvedValue(null);

    const response = await GET(makeRequest());

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toContain("/login");
    expect(buildAuthUrlMock).not.toHaveBeenCalled();
  });

  it("state を `nonce.google_all` 形式で組み立て、nonce のみをcookieへ保存する", async () => {
    getCurrentUserMock.mockResolvedValue({ email: "rep@example.com" });

    const response = await GET(makeRequest());

    expect(response.status).toBe(307);
    expect(buildAuthUrlMock).toHaveBeenCalledTimes(1);
    const state = buildAuthUrlMock.mock.calls[0][0] as string;
    expect(state).toMatch(/^[0-9a-f]{32}\.google_all$/);

    const [nonce] = state.split(".");
    const cookie = response.cookies.get("gmail_oauth_state");
    expect(cookie?.value).toBe(nonce);
    expect(cookie?.path).toBe("/gmail/oauth");
    expect(cookie?.httpOnly).toBe(true);
    expect(cookie?.sameSite).toBe("lax");
  });
});
