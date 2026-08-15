import nodemailer from "nodemailer";

// web-engagement-toolのlib/email.tsを簡略化して移植(2026-08-15)。Gmail OAuth接続の
// 管理機能までは移植せず、SMTP_HOST/_USER/_PASS/_FROM環境変数のみをサポートする
// (招待メール・パスワード再設定・2FAコード送信という低頻度な用途に対しては十分)。
export async function sendEmail(params: {
  to: string | string[];
  subject: string;
  text?: string;
  html?: string;
}): Promise<void> {
  const host = process.env.SMTP_HOST;
  const port = Number(process.env.SMTP_PORT ?? 587);
  const user = process.env.SMTP_USER;
  const pass = process.env.SMTP_PASS;
  const from = process.env.SMTP_FROM ?? user;

  if (!host || !user || !pass || !from) {
    // No SMTP configured — don't block the flow (dev/local usage), just
    // surface the content so whoever is testing can grab the link from logs.
    console.warn(
      "SMTP not configured (SMTP_HOST/_USER/_PASS/_FROM) — email not sent.\n" +
        `To: ${params.to}\nSubject: ${params.subject}\n${params.text ?? params.html ?? ""}`
    );
    return;
  }

  const transporter = nodemailer.createTransport({
    host,
    port,
    secure: port === 465,
    auth: { user, pass },
  });

  await transporter.sendMail({
    from,
    to: params.to,
    subject: params.subject,
    text: params.text,
    html: params.html,
  });
}
