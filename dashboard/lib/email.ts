import nodemailer from "nodemailer";
import MailComposer from "nodemailer/lib/mail-composer";

export const EMAIL_DELIVERY_ERROR = "メールを送信できませんでした。送信設定を確認してから、もう一度お試しください。";

/** 認証情報・メール本文を例外やログへ出さない。 */
export class EmailDeliveryError extends Error {
  constructor() {
    super(EMAIL_DELIVERY_ERROR);
    this.name = "EmailDeliveryError";
  }
}

type Email = { to: string | string[]; subject: string; text?: string; html?: string };

export async function sendEmail(params: Email): Promise<void> {
  try {
    const configured = process.env.SYSTEM_EMAIL_GMAIL_CREDENTIALS_JSON;
    if (configured) {
      // 既存の組織送信元を明示設定する。別アカウントへの自動切替はしない。
      const credentials = JSON.parse(configured);
      for (const key of ["email", "client_id", "client_secret", "refresh_token"]) {
        if (typeof credentials[key] !== "string" || !credentials[key].trim()) throw new EmailDeliveryError();
      }
      if (!/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(credentials.email)) throw new EmailDeliveryError();
      const tokenResponse = await fetch("https://oauth2.googleapis.com/token", {
        method: "POST", redirect: "error", signal: AbortSignal.timeout(10_000),
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ grant_type: "refresh_token", client_id: credentials.client_id,
          client_secret: credentials.client_secret, refresh_token: credentials.refresh_token }),
      });
      if (!tokenResponse.ok) throw new EmailDeliveryError();
      const token = await tokenResponse.json();
      if (typeof token.access_token !== "string" || !token.access_token) throw new EmailDeliveryError();
      const raw = (await new MailComposer({ ...params, from: credentials.email,
        disableFileAccess: true, disableUrlAccess: true }).compile().build()).toString("base64url");
      // 応答不明時に同じPOSTを自動再送しない。
      const sent = await fetch("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {
        method: "POST", redirect: "error", signal: AbortSignal.timeout(20_000),
        headers: { Authorization: `Bearer ${token.access_token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ raw }),
      });
      if (!sent.ok) throw new EmailDeliveryError();
      const result = await sent.json();
      if (typeof result.id !== "string" || !result.id.trim()) throw new EmailDeliveryError();
      return;
    }

    const host = process.env.SMTP_HOST;
    const user = process.env.SMTP_USER;
    const pass = process.env.SMTP_PASS;
    const from = process.env.SMTP_FROM ?? user;
    const port = Number(process.env.SMTP_PORT ?? 587);
    if (!host || !user || !pass || !from) throw new EmailDeliveryError();
    const transporter = nodemailer.createTransport({ host, port, secure: port === 465,
      auth: { user, pass }, connectionTimeout: 10_000, greetingTimeout: 10_000, socketTimeout: 20_000 });
    const result = await transporter.sendMail({ ...params, from, disableFileAccess: true, disableUrlAccess: true });
    if (!result.accepted?.length || result.rejected?.length) throw new EmailDeliveryError();
  } catch {
    // SDK/HTTPの元例外には秘密値が含まれ得るため、原因も添付しない。
    throw new EmailDeliveryError();
  }
}
