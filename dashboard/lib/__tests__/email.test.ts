import { afterEach, describe, expect, it, vi } from "vitest";
import { sendEmail, EmailDeliveryError } from "../email";
const mail = { to: "recipient@example.test", subject: "確認コード", text: "機密本文" };
const credentials = { email: "sender@example.test", client_id: "client", client_secret: "secret", refresh_token: "refresh" };
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("認証メールの実送信と失敗", () => {
  it("設定不足では失敗し、宛先・本文・コードをログへ出さない", async () => {
    for (const key of ["SYSTEM_EMAIL_GMAIL_CREDENTIALS_JSON", "SMTP_HOST", "SMTP_USER", "SMTP_PASS"]) vi.stubEnv(key, "");
    const warn = vi.spyOn(console, "warn");
    await expect(sendEmail(mail)).rejects.toThrow(EmailDeliveryError);
    expect(warn).not.toHaveBeenCalled();
  });
  it("固定送信元で日本語メールを送り、Gmailの受付IDを確認する", async () => {
    vi.stubEnv("SYSTEM_EMAIL_GMAIL_CREDENTIALS_JSON", JSON.stringify(credentials));
    const request = vi.fn().mockResolvedValueOnce(Response.json({ access_token: "access" })).mockResolvedValueOnce(Response.json({ id: "mail-id" }));
    vi.stubGlobal("fetch", request);
    await sendEmail(mail);
    expect(request).toHaveBeenCalledTimes(2);
    const mime = Buffer.from(JSON.parse(request.mock.calls[1][1].body).raw, "base64url").toString();
    expect(mime).toContain("From: sender@example.test");
    expect(mime).toContain("To: recipient@example.test");
    expect(request.mock.calls[1][1].redirect).toBe("error");
  });
  it.each(["token", "send", "missing-id"])("%sの失敗は秘密値を含めず返し、自動再送しない", async (where) => {
    vi.stubEnv("SYSTEM_EMAIL_GMAIL_CREDENTIALS_JSON", JSON.stringify(credentials));
    const request = vi.fn();
    if (where === "token") request.mockRejectedValueOnce(new Error("secret refresh 機密本文"));
    else {
      request.mockResolvedValueOnce(Response.json({ access_token: "access" }));
      if (where === "send") request.mockRejectedValueOnce(new Error("access 機密本文"));
      else request.mockResolvedValueOnce(Response.json({}));
    }
    vi.stubGlobal("fetch", request);
    await expect(sendEmail(mail)).rejects.toEqual(new EmailDeliveryError());
    expect(request).toHaveBeenCalledTimes(where === "token" ? 1 : 2);
  });
  it("不正なGoogle設定をSMTPへ自動切替しない", async () => {
    vi.stubEnv("SYSTEM_EMAIL_GMAIL_CREDENTIALS_JSON", "{}");
    const request = vi.fn(); vi.stubGlobal("fetch", request);
    await expect(sendEmail(mail)).rejects.toThrow(EmailDeliveryError);
    expect(request).not.toHaveBeenCalled();
  });
});
