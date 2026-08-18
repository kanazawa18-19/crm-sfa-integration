import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildAuthUrl, exchangeCodeForRefreshToken } from "@/lib/driveOauth";

const ORIGINAL_ENV = { ...process.env };

describe("driveOauth", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.APP_BASE_URL = "https://crm-sfa-integration-dashboard.vercel.app";
    process.env.GOOGLE_OAUTH_CLIENT_ID = "client-id";
    process.env.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.unstubAllGlobals();
  });

  describe("buildAuthUrl", () => {
    it("Gmail連携より広いdriveスコープと専用のredirect_uriを指定する", () => {
      const url = new URL(buildAuthUrl("state-123"));

      expect(url.origin + url.pathname).toBe("https://accounts.google.com/o/oauth2/v2/auth");
      expect(url.searchParams.get("scope")).toBe("https://www.googleapis.com/auth/drive");
      expect(url.searchParams.get("redirect_uri")).toBe(
        "https://crm-sfa-integration-dashboard.vercel.app/drive/oauth/callback"
      );
      expect(url.searchParams.get("client_id")).toBe("client-id");
      expect(url.searchParams.get("state")).toBe("state-123");
      expect(url.searchParams.get("access_type")).toBe("offline");
      expect(url.searchParams.get("prompt")).toBe("consent");
    });

    it("APP_BASE_URL未設定時は例外を投げる", () => {
      delete process.env.APP_BASE_URL;
      expect(() => buildAuthUrl("state")).toThrow("APP_BASE_URL is not set");
    });

    it("GOOGLE_OAUTH_CLIENT_ID未設定時は例外を投げる", () => {
      delete process.env.GOOGLE_OAUTH_CLIENT_ID;
      expect(() => buildAuthUrl("state")).toThrow("GOOGLE_OAUTH_CLIENT_ID is not set");
    });
  });

  describe("exchangeCodeForRefreshToken", () => {
    it("トークンエンドポイントへredirect_uriを含めてPOSTし、refresh_tokenを返す", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ refresh_token: "refresh-token-abc" }), { status: 200 })
      );
      vi.stubGlobal("fetch", fetchMock);

      const token = await exchangeCodeForRefreshToken("auth-code");

      expect(token).toBe("refresh-token-abc");
      const [url, options] = fetchMock.mock.calls[0];
      expect(url).toBe("https://oauth2.googleapis.com/token");
      const body = new URLSearchParams(options.body as string);
      expect(body.get("code")).toBe("auth-code");
      expect(body.get("redirect_uri")).toBe(
        "https://crm-sfa-integration-dashboard.vercel.app/drive/oauth/callback"
      );
      expect(body.get("grant_type")).toBe("authorization_code");
    });

    it("Googleがrefresh_tokenを返さない場合は例外を投げる(再同意が必要)", async () => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({}), { status: 200 })));

      await expect(exchangeCodeForRefreshToken("auth-code")).rejects.toThrow(
        "Google did not return a refresh_token"
      );
    });

    it("トークンエンドポイントがエラーを返した場合は例外を投げる", async () => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("bad request", { status: 400 })));

      await expect(exchangeCodeForRefreshToken("auth-code")).rejects.toThrow(
        "Google token exchange failed: 400"
      );
    });
  });
});
