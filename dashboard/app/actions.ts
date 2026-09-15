"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { randomBytes } from "crypto";
import prisma from "@/lib/prisma";
import {
  createSessionToken,
  COOKIE_NAME,
  hashPassword,
  verifyPassword,
  createPending2FAToken,
  verifyPending2FAToken,
  PENDING_2FA_COOKIE_NAME,
} from "@/lib/adminSession";
import { requireRole } from "@/lib/auth";
import { EMAIL_CHANGE_DISABLED_MESSAGE, GOOGLE_ONLY_LOGIN_MESSAGE, INVITE_DOMAIN_REJECTED_MESSAGE, isActivatedUser, isAllowedLoginEmail } from "@/lib/loginPolicy";
// ログインセッションの確立は lib/loginSession.ts に置いている。
// このファイルは "use server" なので、ここから export するとクライアントから
// 任意のuserIdで呼べる公開Server Functionになってしまうため（2026-08-31）。
import { establishSession, sendEmailOtpCode } from "@/lib/loginSession";
import { sendEmail } from "@/lib/email";
import { encryptToken, decryptToken } from "@/lib/tokenCrypto";
import { validateAvatarFile } from "@/lib/avatar";
import { EMAIL_REMINDER_THRESHOLD_OPTIONS } from "@/lib/emailReminderThresholds";
import {
  verifyTotpCode,
  generateBackupCodes,
  consumeBackupCode,
  generateEmailOtpPlaintext,
  EMAIL_OTP_TTL_MS,
  EMAIL_OTP_RESEND_COOLDOWN_MS,
} from "@/lib/twoFactor";

// web-engagement-toolのsrc/app/admin/actions.tsのログイン・2FA・パスワード再設定・
// ユーザー管理まわりを移植(2026-08-15)。
// 2026-08-31にGoogleログインも移植した(app/login/google/start と
// app/gmail/oauth/callback の admin_login 分岐)。パスワードでもGoogleでも
// 最終的に lib/loginSession.ts の establishSessionForUser() を通り、2FAの分岐は共通になる。
// **セッション確立の関数をこのファイルから export しないこと。**
// "use server" のファイルから export した関数はクライアントから呼べる公開APIになる。

/**
 * パスワードログインは廃止した（2026-09-16 本人指示。ログインは cnctor.jp の
 * Google アカウントだけ）。ログイン画面からフォームは消してあるが、
 * "use server" から export した関数は誰でも呼べる公開エンドポイントなので、
 * ここでも必ず拒否する。入力は読まず、DB にも触らない。
 * Google ログインは app/gmail/oauth/callback の admin_login 分岐。
 */
export async function login(_prevState: string | undefined, _formData: FormData) {
  return GOOGLE_ONLY_LOGIN_MESSAGE;
}

export async function logout() {
  const cookieStore = await cookies();
  cookieStore.delete(COOKIE_NAME);
  redirect("/login");
}

/** login()が設定した保留中2FA用cookieを読む。無い/期限切れならログイン画面へ。 */
async function requirePending2FAUser() {
  const cookieStore = await cookies();
  const pending = verifyPending2FAToken(cookieStore.get(PENDING_2FA_COOKIE_NAME)?.value);
  if (!pending) redirect("/login");

  const user = await prisma.user.findUnique({ where: { id: pending.userId } });
  if (!user) redirect("/login");
  return user;
}

export async function chooseEmailOtpMethod(_prevState: void | undefined, _formData: FormData) {
  const user = await requirePending2FAUser();

  await prisma.user.update({ where: { id: user.id }, data: { emailOtpEnabled: true } });
  await sendEmailOtpCode(user.id);
  redirect("/login/2fa-email");
}

export async function verifyEmailOtpLogin(_prevState: string | undefined, formData: FormData) {
  const code = String(formData.get("code") ?? "").trim();
  const user = await requirePending2FAUser();

  if (!user.emailOtpEnabled) {
    redirect("/login/2fa-setup");
  }

  const record = await prisma.emailOtpCode.findFirst({
    where: { userId: user.id, usedAt: null, expiresAt: { gt: new Date() } },
    orderBy: { createdAt: "desc" },
  });

  if (!code || !record || !verifyPassword(code, record.codeHash)) {
    return "コードが正しくないか、期限切れです";
  }

  await prisma.emailOtpCode.update({ where: { id: record.id }, data: { usedAt: new Date() } });

  const cookieStore = await cookies();
  cookieStore.delete(PENDING_2FA_COOKIE_NAME);
  await establishSession(user.id);
  redirect("/");
}

export async function resendEmailOtpCode(_prevState: string | undefined, _formData: FormData) {
  const user = await requirePending2FAUser();

  const latest = await prisma.emailOtpCode.findFirst({
    where: { userId: user.id },
    orderBy: { createdAt: "desc" },
  });
  const tooSoon = latest && Date.now() - latest.createdAt.getTime() < EMAIL_OTP_RESEND_COOLDOWN_MS;
  if (tooSoon) {
    return "少し時間をおいてから再送してください";
  }

  await sendEmailOtpCode(user.id);
  redirect("/login/2fa-email");
}

export async function verifyTotpLogin(_prevState: string | undefined, formData: FormData) {
  const code = String(formData.get("code") ?? "").trim();
  const user = await requirePending2FAUser();

  if (!user.totpEnabled || !user.totpSecretEnc) {
    redirect("/login/2fa-setup");
  }

  let ok = verifyTotpCode(decryptToken(user.totpSecretEnc), code);
  if (!ok && code) {
    const remaining = consumeBackupCode(user.totpBackupCodesHash, code);
    if (remaining) {
      await prisma.user.update({ where: { id: user.id }, data: { totpBackupCodesHash: remaining } });
      ok = true;
    }
  }
  if (!ok) {
    return "認証コードが正しくありません";
  }

  const cookieStore = await cookies();
  cookieStore.delete(PENDING_2FA_COOKIE_NAME);
  await establishSession(user.id);
  redirect("/");
}

export type TotpEnrollState = { error?: string; backupCodes?: string[] };

export async function confirmTotpEnrollment(
  _prevState: TotpEnrollState | undefined,
  formData: FormData
): Promise<TotpEnrollState> {
  const secret = String(formData.get("secret") ?? "");
  const code = String(formData.get("code") ?? "").trim();
  const user = await requirePending2FAUser();

  if (!secret || !verifyTotpCode(secret, code)) {
    return { error: "認証コードが正しくありません" };
  }

  const { plaintext, hashes } = generateBackupCodes();
  await prisma.user.update({
    where: { id: user.id },
    data: { totpSecretEnc: encryptToken(secret), totpEnabled: true, totpBackupCodesHash: hashes },
  });

  const cookieStore = await cookies();
  cookieStore.delete(PENDING_2FA_COOKIE_NAME);
  await establishSession(user.id);

  return { backupCodes: plaintext };
}

const RESET_TOKEN_TTL_MS = 1000 * 60 * 60; // 1 hour

/**
 * パスワード再設定は廃止した（2026-09-16、パスワードログイン廃止に伴う）。
 * 残しておくと、誰でも登録メール宛に再設定メールを送りつけられる（フィッシングの下地）うえ、
 * 設定したパスワードはログインに使えず利用者が迷う（シロクマの WARN 2 件）。
 * Server Action は公開エンドポイントなので、画面だけでなくここで拒否する。DB・メールに触らない。
 */
export async function requestPasswordReset(_prevState: string | undefined, _formData: FormData) {
  return GOOGLE_ONLY_LOGIN_MESSAGE;
}

/** 同上。旧い再設定リンクを踏んでも何も更新しない。 */
export async function setPassword(_prevState: string | undefined, _formData: FormData) {
  return GOOGLE_ONLY_LOGIN_MESSAGE;
}

// --- ユーザー管理 ------------------------------------------------------------

export async function inviteUser(_prevState: string | undefined, formData: FormData) {
  await requireRole("master");

  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  const role = String(formData.get("role") ?? "viewer") as "master" | "editor" | "viewer";
  if (!email) return "メールアドレスを入力してください";
  // ログインできるのは cnctor.jp の Google アカウントだけなので、他ドメインを招待しても
  // 一生ログインできない行が User 表に増えるだけ。入口で止める（2026-09-16）。
  if (!isAllowedLoginEmail(email)) return INVITE_DOMAIN_REJECTED_MESSAGE;

  const baseUrl = process.env.APP_BASE_URL ?? "http://localhost:3000";
  // 招待し直しは「Google の紐付けをやり直す」操作でもある（googleSubject を解除）。
  // Google アカウントを作り直した人は、これが無いと永久にログインできない
  // （ChatGPT レビュー BLOCKER：mismatch で拒否されるのに解除経路が無かった）。
  await prisma.user.upsert({
    where: { email },
    update: { role, googleSubject: null },
    create: { email, role, passwordHash: null },
  });

  // パスワード設定リンクは送らない（パスワードログインは廃止）。
  // 招待された人は User 表に載った時点で、Google でログインできる。
  await sendEmail({
    to: email,
    subject: "【営業管理ダッシュボード】管理画面への招待",
    text: `管理画面に招待されました。cnctor.jp の Google アカウントで、以下のURLの「Googleでログイン」からログインしてください。\n\n${baseUrl}/login`,
  });

  redirect("/users");
}

export async function changeUserRole(formData: FormData) {
  await requireRole("master");

  const id = String(formData.get("id") ?? "");
  const role = String(formData.get("role") ?? "") as "master" | "editor" | "viewer";
  if (!id || !role) return;

  await prisma.user.update({ where: { id }, data: { role } });
  redirect("/users");
}

export async function toggleUserIsManager(formData: FormData) {
  await requireRole("master");

  const id = String(formData.get("id") ?? "");
  if (!id) return;

  const target = await prisma.user.findUnique({ where: { id } });
  if (!target) return;

  // 最後の1人をOFFにすると、重大インシデント検知やレコード作成異常のSlack DM通知が
  // 誰にも届かなくなる(サイレント障害)。UI側でも最後の1人のOFF操作は隠しているが、
  // deleteUser()の「有効なmasterアカウントは削除不可」と同じ考え方でサーバー側にも
  // ガードを置く(obasan-qualityレビュー指摘、2026-08-25)。
  if (target.isManager) {
    const otherManagerCount = await prisma.user.count({ where: { isManager: true, id: { not: id } } });
    if (otherManagerCount === 0) return;
  }

  await prisma.user.update({ where: { id }, data: { isManager: !target.isManager } });
  redirect("/users");
}

export async function deleteUser(formData: FormData) {
  const actor = await requireRole("master");

  const id = String(formData.get("id") ?? "");
  if (!id || id === actor.id) return; // can't delete your own account

  const target = await prisma.user.findUnique({ where: { id } });
  if (!target) return;
  // Active admins can't be deleted by anyone, including other admins. A
  // master invite that hasn't been accepted yet (passwordHash still null)
  // isn't an active account though — it's just a stuck invitation — so it
  // stays cancelable like any other pending invite (web-engagement-tool側の
  // 同じ修正をここでも最初から反映、2026-08-15)。
  // 「有効」の判定は isActivatedUser（passwordHash / lastLoginAt / googleSubject）。
  // Google だけのログインでは passwordHash が立たないため（2026-09-16）。
  if (target.role === "master" && isActivatedUser(target)) return;

  await prisma.user.delete({ where: { id } }).catch(() => null);
  redirect("/users");
}

// --- セキュリティ設定 ---------------------------------------------------------

export async function updateSecuritySettings(formData: FormData) {
  await requireRole("master");

  const twoFactorEnabled = formData.get("twoFactorEnabled") === "on";
  const ipAllowlistEnabled = formData.get("ipAllowlistEnabled") === "on";
  const ipAllowlistRaw = String(formData.get("ipAllowlist") ?? "");
  const ipAllowlist = ipAllowlistRaw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  await prisma.appSettings.upsert({
    where: { id: 1 },
    update: { twoFactorEnabled, ipAllowlistEnabled, ipAllowlist },
    create: { id: 1, twoFactorEnabled, ipAllowlistEnabled, ipAllowlist },
  });

  redirect("/settings/security");
}

// --- 未返信メールリマインド設定(/settings/email-reminders) --------------------------
// 実際の判定・送信はPython側(src/email_reminders/reminder_check.py、GitHub Actionsから
// 1時間おき)が行う。ここではAppSettings.emailReminderEnabled/emailReminderThresholdHours
// の保存のみを担う(2026-08-16)。

export async function updateEmailReminderSettings(formData: FormData) {
  await requireRole("master");

  const emailReminderEnabled = formData.get("emailReminderEnabled") === "on";
  // 想定外の値(フォーム外から不正なvalueを送られた場合)が紛れ込まないよう、
  // 許可された選択肢(EMAIL_REMINDER_THRESHOLD_OPTIONS)に含まれるものだけを採用する。
  const emailReminderThresholdHours = formData
    .getAll("emailReminderThresholdHours")
    .map((value) => Number(value))
    .filter((value) => EMAIL_REMINDER_THRESHOLD_OPTIONS.includes(value));

  await prisma.appSettings.upsert({
    where: { id: 1 },
    update: { emailReminderEnabled, emailReminderThresholdHours },
    create: { id: 1, emailReminderEnabled, emailReminderThresholdHours },
  });

  redirect("/settings/email-reminders");
}

// --- 自分のプロフィール編集(/settings/profile) ---------------------------------
// 管理者が他人を編集する機能ではなく、ログイン中の本人が自分の情報を編集するための
// アクション群(2026-08-16)。ロールに関係なく本人であればよいのでrequireRole("viewer")
// (=最低ロール、実質「ログイン済みなら誰でも」)で認可する。

export type ProfileActionState = { error?: string; success?: string };

export async function updateOwnProfile(
  _prevState: ProfileActionState | undefined,
  formData: FormData
): Promise<ProfileActionState> {
  const user = await requireRole("viewer");
  const name = String(formData.get("name") ?? "").trim();
  const title = String(formData.get("title") ?? "").trim();
  const department = String(formData.get("department") ?? "").trim();
  if (name.length > 100 || title.length > 100 || department.length > 100) {
    return { error: "各項目は100文字以内で入力してください" };
  }

  await prisma.user.update({
    where: { id: user.id },
    data: { name: name || null, title: title || null, department: department || null },
  });
  revalidatePath("/settings/profile");
  return { success: "プロフィールを更新しました" };
}

const EMAIL_CHANGE_TOKEN_TTL_MS = 1000 * 60 * 60; // 1 hour、パスワード再設定と同じ有効期限

/**
 * メールアドレス変更・パスワード変更は廃止した（2026-09-16、ログインを cnctor.jp の Google
 * アカウントだけにしたため）。画面から外しただけでは "use server" の公開 Server Action が
 * 残り、直接呼べばメールを別アドレスに変えられる（ChatGPT レビュー BLOCKER）。
 * 入力を読まず DB にも触らず拒否する。アドレスを変えたいときは、管理者がユーザー管理から
 * 新しいアドレスで招待し直す。
 */
export async function requestOwnEmailChange(
  _prevState: ProfileActionState | undefined,
  _formData: FormData
): Promise<ProfileActionState> {
  return { error: EMAIL_CHANGE_DISABLED_MESSAGE };
}

/** 同上。旧い確認リンクを踏んでも何も更新しない。 */
export async function confirmEmailChange(_prevState: string | undefined, _formData: FormData) {
  return EMAIL_CHANGE_DISABLED_MESSAGE;
}

/** 同上。パスワードはログインに使えないので変更も受け付けない。 */
export async function changeOwnPassword(
  _prevState: ProfileActionState | undefined,
  _formData: FormData
): Promise<ProfileActionState> {
  return { error: GOOGLE_ONLY_LOGIN_MESSAGE };
}

export async function updateOwnAvatar(
  _prevState: ProfileActionState | undefined,
  formData: FormData
): Promise<ProfileActionState> {
  const user = await requireRole("viewer");
  const file = formData.get("avatar");
  if (!(file instanceof File)) {
    return { error: "画像ファイルを選択してください" };
  }

  const validationError = validateAvatarFile(file);
  if (validationError) {
    return { error: validationError };
  }

  let url: string;
  try {
    // ビルド時にBLOB_READ_WRITE_TOKENが未設定でもnext buildが失敗しないよう、
    // モジュールのトップレベルではなくこのアクション内でのみ動的importする。
    const { put } = await import("@vercel/blob");
    const ext = file.type === "image/png" ? "png" : file.type === "image/webp" ? "webp" : "jpg";
    const blob = await put(`avatars/${user.id}.${ext}`, file, {
      access: "public",
      addRandomSuffix: false,
      allowOverwrite: true,
    });
    url = blob.url;
  } catch (error) {
    console.error("avatar upload failed", error);
    return { error: "画像のアップロードに失敗しました。時間をおいて再度お試しください。" };
  }

  await prisma.user.update({ where: { id: user.id }, data: { avatarUrl: url } });
  revalidatePath("/settings/profile");
  return { success: "アイコン画像を更新しました" };
}
