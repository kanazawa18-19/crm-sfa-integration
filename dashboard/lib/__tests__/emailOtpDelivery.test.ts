import { beforeEach, describe, expect, it, vi } from "vitest";
const mock = vi.hoisted(() => ({ create: vi.fn(), update: vi.fn(), user: vi.fn(), send: vi.fn() }));
vi.mock("next/headers", () => ({ cookies: vi.fn() }));
vi.mock("@/lib/prisma", () => ({ default: { emailOtpCode: { create: mock.create, update: mock.update }, user: { findUnique: mock.user } } }));
vi.mock("@/lib/email", () => ({ sendEmail: mock.send }));
vi.mock("@/lib/adminSession", () => ({ hashPassword: () => "hashed", COOKIE_NAME: "session", PENDING_2FA_COOKIE_NAME: "pending", createPending2FAToken: vi.fn(), createSessionToken: vi.fn() }));
vi.mock("@/lib/twoFactor", () => ({ EMAIL_OTP_TTL_MS: 600000, generateEmailOtpPlaintext: () => "123456" }));
import { sendEmailOtpCode } from "../loginSession";
describe("認証メール送信失敗時の失効", () => {
  beforeEach(() => { vi.resetAllMocks(); mock.create.mockResolvedValue({ id: "new-otp" }); mock.user.mockResolvedValue({ email: "recipient@example.test" }); });
  it("失敗した今回のコードだけを失効させる", async () => {
    mock.send.mockRejectedValue(new Error("送信失敗"));
    await expect(sendEmailOtpCode("user-1")).rejects.toThrow("送信失敗");
    expect(mock.update).toHaveBeenCalledWith({ where: { id: "new-otp" }, data: { usedAt: expect.any(Date) } });
  });
  it("送信成功したコードは本人が入力できる状態を維持する", async () => {
    mock.send.mockResolvedValue(undefined);
    await sendEmailOtpCode("user-1");
    expect(mock.update).not.toHaveBeenCalled();
    expect(mock.send).toHaveBeenCalledWith(expect.objectContaining({ to: "recipient@example.test" }));
  });
});
