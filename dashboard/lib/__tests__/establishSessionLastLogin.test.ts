import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * ログイン成立時に User.lastLoginAt を記録すること（有効な管理者の削除保護の要）。
 * 生 SQL（$executeRaw）で書き、監査対象の user.update は通さない（毎回のログインを AuditLog に積まない）。
 * Gemini レビュー（2026-09-16）「lastLoginAt の更新そのもののテストが無い」への対応。
 */

const executeRawMock = vi.fn();
const userUpdateMock = vi.fn();
const cookieSetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: async () => ({ set: cookieSetMock, get: () => undefined, delete: vi.fn() }),
}));
vi.mock("next/navigation", () => ({ redirect: vi.fn() }));
vi.mock("@/lib/prisma", () => ({
  default: {
    $executeRaw: (...args: unknown[]) => executeRawMock(...args),
    user: { findUnique: vi.fn(), update: (...args: unknown[]) => userUpdateMock(...args) },
    appSettings: { findUnique: vi.fn().mockResolvedValue({ twoFactorEnabled: false }) },
  },
}));
vi.mock("@/lib/email", () => ({ sendEmail: vi.fn() }));

const { establishSessionForUser } = await import("@/lib/loginSession");

describe("ログイン成立時の lastLoginAt 記録", () => {
  beforeEach(() => {
    executeRawMock.mockReset();
    userUpdateMock.mockReset();
    cookieSetMock.mockReset();
    process.env.SESSION_SECRET = "test-secret";
  });

  it("2FA が OFF なら、生 SQL で lastLoginAt を書いてからセッション cookie を発行する", async () => {
    const result = await establishSessionForUser("user-1");

    expect(result).toEqual({ needsTwoFactor: false, redirectTo: "/" });
    expect(executeRawMock).toHaveBeenCalledTimes(1);
    const [strings, ...values] = executeRawMock.mock.calls[0] as [TemplateStringsArray, ...unknown[]];
    expect(strings.join("?")).toContain('UPDATE "User" SET "lastLoginAt" = ?');
    expect(values[0]).toBeInstanceOf(Date);
    expect(values[1]).toBe("user-1");
    expect(userUpdateMock).not.toHaveBeenCalled();
    expect(cookieSetMock).toHaveBeenCalledTimes(1);
  });
});
