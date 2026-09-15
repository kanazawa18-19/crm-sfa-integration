import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * パスワードログインの廃止（2026-09-16）。
 * login() は "use server" の公開 Server Action なので、画面からフォームを消すだけでは足りない。
 * 正しいメール・パスワードを渡しても、DB を見ずに拒否することを固定する。
 */

const findUserMock = vi.fn();
const userUpdateMock = vi.fn();
const cookieSetMock = vi.fn();
const sendEmailMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: async () => ({ set: cookieSetMock, get: () => undefined, delete: vi.fn() }),
}));
vi.mock("next/navigation", () => ({ redirect: vi.fn() }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/prisma", () => ({
  default: {
    user: { findUnique: (...args: unknown[]) => findUserMock(...args), update: (...args: unknown[]) => userUpdateMock(...args) },
    passwordResetToken: { findUnique: vi.fn(), create: vi.fn() },
    appSettings: { findUnique: vi.fn() },
    $transaction: vi.fn(),
  },
}));
vi.mock("@/lib/email", () => ({ sendEmail: (...args: unknown[]) => sendEmailMock(...args) }));

const { login, requestPasswordReset, setPassword } = await import("@/app/actions");

describe("パスワードログインの廃止", () => {
  beforeEach(() => {
    findUserMock.mockReset();
    userUpdateMock.mockReset();
    cookieSetMock.mockReset();
    sendEmailMock.mockReset();
  });

  it("メールとパスワードを渡しても拒否し、DB もセッションも触らない", async () => {
    const form = new FormData();
    form.set("email", "taro@cnctor.jp");
    form.set("password", "correct-password");

    const result = await login(undefined, form);

    expect(result).toContain("パスワードでのログインは廃止");
    expect(result).toContain("cnctor.jp");
    expect(findUserMock).not.toHaveBeenCalled();
    expect(cookieSetMock).not.toHaveBeenCalled();
  });

  it("パスワード再設定の依頼も拒否し、メールを送らず DB も触らない", async () => {
    const form = new FormData();
    form.set("email", "taro@cnctor.jp");

    const result = await requestPasswordReset(undefined, form);

    expect(result).toContain("パスワードでのログインは廃止");
    expect(findUserMock).not.toHaveBeenCalled();
    expect(sendEmailMock).not.toHaveBeenCalled();
  });

  it("旧い再設定リンクでパスワードを設定しようとしても何も更新しない", async () => {
    const form = new FormData();
    form.set("token", "old-token");
    form.set("password", "new-password-123");

    const result = await setPassword(undefined, form);

    expect(result).toContain("パスワードでのログインは廃止");
    expect(findUserMock).not.toHaveBeenCalled();
    expect(userUpdateMock).not.toHaveBeenCalled();
  });
});
