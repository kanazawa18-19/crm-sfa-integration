import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { POST } from "@/app/api/login/route";
import { SESSION_COOKIE_NAME } from "@/lib/auth";

const ORIGINAL_ENV = { ...process.env };

function makeRequest(body: unknown): Request {
  const init: RequestInit =
    body === undefined
      ? { method: "POST" }
      : {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: typeof body === "string" ? body : JSON.stringify(body),
        };
  return new Request("http://localhost/api/login", init);
}

describe("POST /api/login", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.DASHBOARD_PASSWORD = "correct-password";
    process.env.SESSION_SECRET = "test-secret";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
  });

  it("正しいパスワードで200・cookieが発行される", async () => {
    const response = await POST(makeRequest({ password: "correct-password" }));
    expect(response.status).toBe(200);
    const cookie = response.cookies.get(SESSION_COOKIE_NAME);
    expect(cookie?.value).toBeTruthy();
  });

  it("誤ったパスワードで401", async () => {
    const response = await POST(makeRequest({ password: "wrong-password" }));
    expect(response.status).toBe(401);
  });

  it("DASHBOARD_PASSWORD 未設定時は常に401（fail-closed）", async () => {
    delete process.env.DASHBOARD_PASSWORD;
    const response = await POST(makeRequest({ password: "correct-password" }));
    expect(response.status).toBe(401);
  });

  it("不正なリクエストボディ（JSON以外）で400を返す", async () => {
    const response = await POST(makeRequest("not-json"));
    expect(response.status).toBe(400);
  });
});
