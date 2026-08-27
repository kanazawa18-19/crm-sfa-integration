import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildAuthUrl, exchangeCodeForRefreshToken } from "@/lib/googleOauth";

const ORIGINAL_ENV = { ...process.env };

describe("googleOauth", () => {
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
    it("gmail.readonlyとdriveの両スコープを1つのURLで要求し、redirect_uriはGmail用callbackを再利用する", () => {
      const url = new URL(buildAuthUrl("nonce-abc.google_all"));

      expect(url.origin + url.pathname).toBe("https://accounts.google.com/o/oauth2/v2/auth");
      expect(url.searchParams.get("scope")).toBe(
        "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/drive"
      );
      // 新しいredirect_uriをGoogle Cloud Consoleに追加登録しなくて済むように、
      // 既存のGmail用コールバックをそのまま使う(app/gmail/oauth/callback)。
      expect(url.searchParams.get("redirect_uri")).toBe(
        "https://crm-sfa-integration-dashboard.vercel.app/gmail/oauth/callback"
      );
      expect(url.searchParams.get("state")).toBe("nonce-abc.google_all");
      expect(url.searchParams.get("access_type")).toBe("offline");
      expect(url.searchParams.get("prompt")).toBe("consent");
    });
  });

  describe("exchangeCodeForRefreshToken", () => {
    it("gmailOauth.tsのトークン交換をそのまま再利用し、Gmail用redirect_uriでPOSTする", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ refresh_token: "refresh-token-xyz" }), { status: 200 })
      );
      vi.stubGlobal("fetch", fetchMock);

      const token = await exchangeCodeForRefreshToken("auth-code");

      expect(token).toBe("refresh-token-xyz");
      const [, options] = fetchMock.mock.calls[0];
      const body = new URLSearchParams(options.body as string);
      expect(body.get("redirect_uri")).toBe(
        "https://crm-sfa-integration-dashboard.vercel.app/gmail/oauth/callback"
      );
    });
  });
});
