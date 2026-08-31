import { describe, expect, it } from "vitest";
import {
  LOGIN_STATE_COOKIE,
  LOGIN_STATE_COOKIE_PATH,
} from "@/app/login/google/start/route";

/**
 * nonce cookie の path（2026-08-31、Geminiのレビュー指摘）。
 *
 * cookieは `path="/gmail/oauth"` で発行しているが、Next.jsの `cookies.delete(name)` は
 * **既定で path="/" を対象にする**。pathを渡さないと古いnonceがブラウザに残り、
 * 「nonceは使い捨て」という前提が崩れる。発行と削除で同じ定数を使うことで、
 * 片方だけ変えたときに気づけるようにする。
 */
describe("Googleログインのnonce cookie", () => {
  it("発行と削除で同じpathを使う", () => {
    expect(LOGIN_STATE_COOKIE_PATH).toBe("/gmail/oauth");
  });

  it("連携フローのcookieとは別名にする（互いのnonceを上書きしないため）", () => {
    expect(LOGIN_STATE_COOKIE).toBe("admin_login_oauth_state");
    expect(LOGIN_STATE_COOKIE).not.toBe("gmail_oauth_state");
  });
});
