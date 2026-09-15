import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * メールアドレス変更・パスワード変更の廃止（2026-09-16、ChatGPT レビュー BLOCKER）。
 * 画面から外しただけでは "use server" の公開 Server Action が残るので、直接呼んでも拒否することを固定する。
 */

const findUserMock = vi.fn();
const userUpdateMock = vi.fn();
const sendEmailMock = vi.fn();

vi.mock("next/headers", () => ({ cookies: async () => ({ get: () => undefined, set: vi.fn(), delete: vi.fn() }) }));
vi.mock("next/navigation", () => ({ redirect: vi.fn() }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/auth", () => ({ requireRole: async () => ({ id: "user-1", email: "taro@cnctor.jp", role: "viewer" }) }));
vi.mock("@/lib/prisma", () => ({
  default: {
    user: { findUnique: (...args: unknown[]) => findUserMock(...args), update: (...args: unknown[]) => userUpdateMock(...args) },
    emailChangeToken: { findUnique: vi.fn(), create: vi.fn(), updateMany: vi.fn() },
    passwordResetToken: { findUnique: vi.fn(), create: vi.fn() },
    appSettings: { findUnique: vi.fn() },
    $transaction: vi.fn(),
  },
}));
vi.mock("@/lib/email", () => ({ sendEmail: (...args: unknown[]) => sendEmailMock(...args) }));

const { requestOwnEmailChange, confirmEmailChange, changeOwnPassword } = await import("@/app/actions");

describe("プロフィールのメール変更・パスワード変更の廃止", () => {
  beforeEach(() => {
    findUserMock.mockReset();
    userUpdateMock.mockReset();
    sendEmailMock.mockReset();
  });

  it("メール変更の依頼は拒否し、確認メールを送らない", async () => {
    const form = new FormData();
    form.set("currentPassword", "correct");
    form.set("newEmail", "new@cnctor.jp");

    const result = await requestOwnEmailChange(undefined, form);

    expect(result.error).toContain("メールアドレスの変更は受け付けていません");
    expect(sendEmailMock).not.toHaveBeenCalled();
    expect(userUpdateMock).not.toHaveBeenCalled();
  });

  it("旧い確認リンクを踏んでもメールを更新しない", async () => {
    const form = new FormData();
    form.set("token", "old-token");

    const result = await confirmEmailChange(undefined, form);

    expect(result).toContain("メールアドレスの変更は受け付けていません");
    expect(userUpdateMock).not.toHaveBeenCalled();
  });

  it("パスワード変更は拒否し、DB を触らない", async () => {
    const form = new FormData();
    form.set("currentPassword", "correct");
    form.set("newPassword", "new-password-123");

    const result = await changeOwnPassword(undefined, form);

    expect(result.error).toContain("パスワードでのログインは廃止");
    expect(findUserMock).not.toHaveBeenCalled();
    expect(userUpdateMock).not.toHaveBeenCalled();
  });
});
