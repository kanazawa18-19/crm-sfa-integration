import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

// 配信停止ページがIP制限とログインの両方を素通りできることの検証(2026-09-03)。
//
// proxy.tsのコメントにあるとおり、この1行を下へ動かすとIP制限がONの間だけ
// 配信停止ができなくなる。**社外から開けない配信停止リンクを載せたメールを撒く**のは
// 特定電子メール法に反するので、順番そのものをテストで固定する
// (kuma-qaレビューWARN、それまでproxy.tsにはテストが1本も無かった)。

const findUnique = vi.fn();
vi.mock("@/lib/prisma", () => ({
  default: { appSettings: { findUnique: (...args: unknown[]) => findUnique(...args) } },
}));

const { proxy } = await import("@/proxy");

function request(path: string): NextRequest {
  // IP制限が有効かつ許可リストが空 = このリクエスト元は許可されていない状態。
  return new NextRequest(`https://dash.example.com${path}`, {
    headers: { "x-forwarded-for": "198.51.100.1" },
  });
}

describe("proxy", () => {
  beforeEach(() => {
    findUnique.mockReset().mockResolvedValue({
      id: 1,
      ipAllowlistEnabled: true,
      ipAllowlist: [],
    });
  });

  it("IP制限がONでも配信停止ページは通る", async () => {
    const response = await proxy(request("/unsubscribe"));
    expect(response.status).toBe(200);
  });

  it("IP制限がONでも配信停止の完了画面は通る", async () => {
    const response = await proxy(request("/unsubscribe/done"));
    expect(response.status).toBe(200);
  });

  it("配信停止ページはログインを要求しない（/loginへ飛ばさない）", async () => {
    findUnique.mockResolvedValue({ id: 1, ipAllowlistEnabled: false, ipAllowlist: [] });
    const response = await proxy(request("/unsubscribe"));
    expect(response.headers.get("location")).toBeNull();
  });

  it("社内の画面はIP制限で止まる（バイパスが広がっていないこと）", async () => {
    const response = await proxy(request("/bulk-email"));
    expect(response.status).toBe(403);
  });

  it("配信停止に似た別のパスは通さない（完全一致であること）", async () => {
    const response = await proxy(request("/unsubscribe/evil"));
    expect(response.status).toBe(403);
  });

  it("ログイン済みでなければ社内の画面は/loginへ飛ぶ", async () => {
    findUnique.mockResolvedValue({ id: 1, ipAllowlistEnabled: false, ipAllowlist: [] });
    const response = await proxy(request("/bulk-email"));
    expect(response.headers.get("location")).toContain("/login");
  });
});
