import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildAuthUrl, exchangeCodeForRefreshToken, exchangeCodeForToken, GMAIL_SCOPE } from "@/lib/gmailOauth";

const ORIGINAL_ENV = { ...process.env };

describe("gmailOauth", () => {
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
    it("gmail.readonlyのみをscopeに要求する", () => {
      const url = new URL(buildAuthUrl("nonce-abc"));
      expect(url.searchParams.get("scope")).toBe(GMAIL_SCOPE);
      expect(url.searchParams.get("prompt")).toBe("consent");
    });
  });

  describe("exchangeCodeForToken", () => {
    it("scopeレスポンスをスペース区切りで配列化してgrantedScopesとして返す", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            refresh_token: "refresh-token-xyz",
            scope: "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/drive",
          }),
          { status: 200 }
        )
      );
      vi.stubGlobal("fetch", fetchMock);

      const result = await exchangeCodeForToken("auth-code");

      expect(result.refreshToken).toBe("refresh-token-xyz");
      expect(result.grantedScopes).toEqual([
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive",
      ]);
    });

    it("scopeがレスポンスに含まれない場合はgrantedScopesが空配列になる(スコープ検証側で全許可なしとして扱われる)", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ refresh_token: "refresh-token-xyz" }), { status: 200 })
      );
      vi.stubGlobal("fetch", fetchMock);

      const result = await exchangeCodeForToken("auth-code");

      expect(result.grantedScopes).toEqual([]);
    });

    it("refresh_tokenが無い場合はエラーを投げる", async () => {
      const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({}), { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await expect(exchangeCodeForToken("auth-code")).rejects.toThrow(/refresh_token/);
    });

    it("トークン交換が失敗した場合はエラーを投げる", async () => {
      const fetchMock = vi.fn().mockResolvedValue(new Response("bad request", { status: 400 }));
      vi.stubGlobal("fetch", fetchMock);

      await expect(exchangeCodeForToken("auth-code")).rejects.toThrow(/Google token exchange failed/);
    });
  });

  describe("exchangeCodeForRefreshToken (互換ラッパー)", () => {
    it("exchangeCodeForTokenのrefreshTokenだけを返す", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ refresh_token: "refresh-token-xyz", scope: GMAIL_SCOPE }), { status: 200 })
      );
      vi.stubGlobal("fetch", fetchMock);

      const token = await exchangeCodeForRefreshToken("auth-code");

      expect(token).toBe("refresh-token-xyz");
    });
  });
});
