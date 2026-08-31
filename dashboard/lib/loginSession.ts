// ログインセッションを確立する共通処理（2026-08-31に app/actions.ts から切り出した）。
//
// **なぜ別ファイルにするのか。**
// `app/actions.ts` は先頭が `"use server"` で、そこから export した関数は
// Next.jsが**クライアントから呼べるServer Function（Server Action）として公開する**。
// つまり `establishSessionForUser(userId)` を app/actions.ts で export すると、
// **任意のuserIdを渡してセッションを作れる公開エンドポイント**になり、
// Google OAuthもパスワード検証も丸ごと迂回できてしまう。
// （2026-08-31、ChatGPTのレビューで指摘を受けて修正。Next.jsの公式ドキュメントも
// 「exportしたServer Functionは公開APIと同等に認証・認可せよ」としている。）
//
// このファイルは `"use server"` を付けない通常のモジュールなので、
// import した側（Server ActionとRoute Handler）からしか呼べない。
// `sendEmailOtpCode` も同じ理由でここに移した（元は app/actions.ts から
// export されており、任意のuserId宛に確認コードメールを送れる状態だった）。
import { cookies } from "next/headers";
import prisma from "@/lib/prisma";
import {
  COOKIE_NAME,
  PENDING_2FA_COOKIE_NAME,
  createPending2FAToken,
  createSessionToken,
  hashPassword,
} from "@/lib/adminSession";
import { sendEmail } from "@/lib/email";
import { EMAIL_OTP_TTL_MS, generateEmailOtpPlaintext } from "@/lib/twoFactor";

export async function establishSession(userId: string): Promise<void> {
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, createSessionToken(userId), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
  });
}

export async function sendEmailOtpCode(userId: string): Promise<void> {
  const code = generateEmailOtpPlaintext();
  const codeHash = hashPassword(code);
  await prisma.emailOtpCode.create({
    data: { userId, codeHash, expiresAt: new Date(Date.now() + EMAIL_OTP_TTL_MS) },
  });

  const user = await prisma.user.findUnique({ where: { id: userId } });
  if (!user) return;

  await sendEmail({
    to: user.email,
    subject: "【営業管理ダッシュボード】ログイン確認コード",
    text: `ログイン確認コード: ${code}\n\n10分間有効です。心当たりがない場合はこのメールを無視してください。`,
  });
}

/**
 * 本人確認が済んだ後の共通の分岐。AppSettings.twoFactorEnabledがONなら2FA検証へ、
 * OFFならそのままセッションを確立する。
 *
 * パスワードログイン（`app/actions.ts`の`login()`）とGoogleログイン
 * （`app/gmail/oauth/callback`の`admin_login`分岐）の両方から呼ぶ。
 * **Googleでログインしても2FAを迂回させない**ために、入口を1つにまとめてある。
 *
 * **この関数は「呼ばれた時点で本人確認は済んでいる」ことを前提にしている。**
 * 呼び出し元がパスワード検証かGoogleのメール確認済み判定を必ず先に済ませること。
 * だからこそ、クライアントから直接呼べる場所に置いてはいけない（冒頭の注意書き参照）。
 */
export async function establishSessionForUser(
  userId: string
): Promise<{ needsTwoFactor: boolean; redirectTo: string }> {
  const settings = await prisma.appSettings.findUnique({ where: { id: 1 } });
  if (settings?.twoFactorEnabled) {
    const user = await prisma.user.findUnique({ where: { id: userId } });
    const cookieStore = await cookies();
    cookieStore.set(PENDING_2FA_COOKIE_NAME, createPending2FAToken(userId), {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
    });

    let redirectTo: string;
    if (user?.totpEnabled) {
      redirectTo = "/login/2fa";
    } else if (user?.emailOtpEnabled) {
      await sendEmailOtpCode(user.id);
      redirectTo = "/login/2fa-email";
    } else {
      redirectTo = "/login/2fa-setup";
    }

    return { needsTwoFactor: true, redirectTo };
  }

  await establishSession(userId);
  return { needsTwoFactor: false, redirectTo: "/" };
}
