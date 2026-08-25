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
// ユーザー管理まわりを移植(2026-08-15)。Googleログイン等MA固有の機能は含めない。

async function establishSession(userId: string) {
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, createSessionToken(userId), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
  });
}

/**
 * パスワード認証成功後の分岐。AppSettings.twoFactorEnabledがONなら2FA検証へ、
 * OFFならそのままセッションを確立する。
 */
async function establishSessionForUser(
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

export async function login(_prevState: string | undefined, formData: FormData) {
  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  const password = String(formData.get("password") ?? "");

  const user = await prisma.user.findUnique({ where: { email } });
  if (!user || !verifyPassword(password, user.passwordHash)) {
    return "メールアドレスまたはパスワードが違います";
  }

  const { redirectTo } = await establishSessionForUser(user.id);
  redirect(redirectTo);
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

async function issueResetToken(userId: string): Promise<string> {
  const token = randomBytes(32).toString("hex");
  await prisma.passwordResetToken.create({
    data: { userId, token, expiresAt: new Date(Date.now() + RESET_TOKEN_TTL_MS) },
  });
  return token;
}

export async function requestPasswordReset(_prevState: string | undefined, formData: FormData) {
  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  const baseUrl = process.env.APP_BASE_URL ?? "http://localhost:3000";

  const user = await prisma.user.findUnique({ where: { email } });
  // Always show the same message — don't leak whether the address is registered.
  if (user) {
    const token = await issueResetToken(user.id);
    await sendEmail({
      to: user.email,
      subject: "【営業管理ダッシュボード】パスワード再設定",
      text: `パスワードを再設定するには以下のリンクを開いてください(1時間有効)。\n\n${baseUrl}/set-password?token=${token}\n\n心当たりがない場合はこのメールを無視してください。`,
    });
  }

  return "登録されているメールアドレスであれば、再設定用のリンクを送信しました。";
}

export async function setPassword(_prevState: string | undefined, formData: FormData) {
  const token = String(formData.get("token") ?? "");
  const password = String(formData.get("password") ?? "");
  if (!token || password.length < 8) {
    return "パスワードは8文字以上で入力してください";
  }

  const resetToken = await prisma.passwordResetToken.findUnique({ where: { token } });
  if (!resetToken || resetToken.usedAt || resetToken.expiresAt < new Date()) {
    return "リンクの有効期限が切れています。もう一度お試しください。";
  }

  await prisma.$transaction([
    prisma.user.update({
      where: { id: resetToken.userId },
      data: { passwordHash: hashPassword(password) },
    }),
    prisma.passwordResetToken.update({
      where: { id: resetToken.id },
      data: { usedAt: new Date() },
    }),
  ]);

  redirect("/login");
}

// --- ユーザー管理 ------------------------------------------------------------

export async function inviteUser(_prevState: void | undefined, formData: FormData) {
  await requireRole("master");

  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  const role = String(formData.get("role") ?? "viewer") as "master" | "editor" | "viewer";
  if (!email) return;

  const baseUrl = process.env.APP_BASE_URL ?? "http://localhost:3000";
  const user = await prisma.user.upsert({
    where: { email },
    update: { role },
    create: { email, role, passwordHash: null },
  });

  const token = await issueResetToken(user.id);
  await sendEmail({
    to: email,
    subject: "【営業管理ダッシュボード】管理画面への招待",
    text: `管理画面に招待されました。以下のリンクからパスワードを設定してログインしてください(1時間有効)。\n\n${baseUrl}/set-password?token=${token}`,
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
  if (target.role === "master" && target.passwordHash !== null) return;

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
 * メールアドレス変更はパスワード再設定/招待と同じワンタイムトークン方式。即時上書きは
 * せず、新しいメールアドレス宛に確認リンクを送り、confirmEmailChange()でクリックされて
 * 初めて確定する(乗っ取られたセッションから他人のメールを乗っ取られるのを防ぐ)。
 */
export async function requestOwnEmailChange(
  _prevState: ProfileActionState | undefined,
  formData: FormData
): Promise<ProfileActionState> {
  const user = await requireRole("viewer");
  const newEmail = String(formData.get("newEmail") ?? "").trim().toLowerCase();

  if (!newEmail || !newEmail.includes("@")) {
    return { error: "有効なメールアドレスを入力してください" };
  }
  if (newEmail === user.email) {
    return { error: "現在と同じメールアドレスです" };
  }

  // requestPasswordResetと同じ方針: 入力されたメールアドレスが既に別アカウントで
  // 使われているかどうかをレスポンスの文言から判別できないよう、常に同じ成功
  // メッセージを返す(そのアドレスが既に使われている場合はメールを送らないだけで、
  // 呼び出し側にはエラーとして伝えない — shirokuma-secレビュー指摘、2026-08-16)。
  const successMessage = `${newEmail} が未登録であれば、確認メールを送信しました。メール内のリンクを開いて変更を確定してください。`;

  const existing = await prisma.user.findUnique({ where: { email: newEmail } });
  if (existing) {
    return { success: successMessage };
  }

  // 同一ユーザーの未使用トークンを無効化してから新しく発行する。古いリクエストの
  // リンクが宛先を変えて有効なまま残り続けるのを防ぐ(shirokuma-secレビュー指摘、
  // 2026-08-16)。
  await prisma.emailChangeToken.updateMany({
    where: { userId: user.id, usedAt: null },
    data: { usedAt: new Date() },
  });

  const token = randomBytes(32).toString("hex");
  await prisma.emailChangeToken.create({
    data: {
      userId: user.id,
      newEmail,
      token,
      expiresAt: new Date(Date.now() + EMAIL_CHANGE_TOKEN_TTL_MS),
    },
  });

  const baseUrl = process.env.APP_BASE_URL ?? "http://localhost:3000";
  await sendEmail({
    to: newEmail,
    subject: "【営業管理ダッシュボード】メールアドレス変更の確認",
    text: `メールアドレスを変更するには以下のリンクを開いてください(1時間有効)。\n\n${baseUrl}/confirm-email-change?token=${token}\n\n心当たりがない場合はこのメールを無視してください。`,
  });

  return { success: successMessage };
}

export async function confirmEmailChange(_prevState: string | undefined, formData: FormData) {
  const token = String(formData.get("token") ?? "");
  if (!token) return "リンクが不正です";

  const changeToken = await prisma.emailChangeToken.findUnique({ where: { token } });
  if (!changeToken || changeToken.usedAt || changeToken.expiresAt < new Date()) {
    return "リンクの有効期限が切れています。もう一度お試しください。";
  }

  // 発行後、確定前に他ユーザーがそのメールアドレスを取得している可能性もゼロではない
  // ため、確定直前にもう一度重複チェックする。
  const existing = await prisma.user.findUnique({ where: { email: changeToken.newEmail } });
  if (existing && existing.id !== changeToken.userId) {
    return "このメールアドレスは既に使用されています";
  }

  const targetUser = await prisma.user.findUnique({ where: { id: changeToken.userId } });
  const oldEmail = targetUser?.email;

  await prisma.$transaction([
    prisma.user.update({ where: { id: changeToken.userId }, data: { email: changeToken.newEmail } }),
    prisma.emailChangeToken.update({ where: { id: changeToken.id }, data: { usedAt: new Date() } }),
  ]);

  // 変更前のメールアドレス宛に通知する — 自分がリクエストしていない変更(乗っ取り)
  // に本人が気づける最後の砦(shirokuma-secレビュー指摘、2026-08-16)。
  if (oldEmail) {
    await sendEmail({
      to: oldEmail,
      subject: "【営業管理ダッシュボード】メールアドレスが変更されました",
      text: `このアカウントのメールアドレスが ${changeToken.newEmail} に変更されました。\n\n心当たりがない場合は至急管理者にご連絡ください。`,
    });
  }

  redirect("/settings/profile?emailChanged=1");
}

/** 現在のパスワード入力を必須にすることで、乗っ取られたセッションからの変更を防ぐ。 */
export async function changeOwnPassword(
  _prevState: ProfileActionState | undefined,
  formData: FormData
): Promise<ProfileActionState> {
  const user = await requireRole("viewer");
  const currentPassword = String(formData.get("currentPassword") ?? "");
  const newPassword = String(formData.get("newPassword") ?? "");

  if (newPassword.length < 8) {
    return { error: "新しいパスワードは8文字以上で入力してください" };
  }

  const freshUser = await prisma.user.findUnique({ where: { id: user.id } });
  if (!freshUser || !verifyPassword(currentPassword, freshUser.passwordHash)) {
    return { error: "現在のパスワードが正しくありません" };
  }

  await prisma.user.update({ where: { id: user.id }, data: { passwordHash: hashPassword(newPassword) } });
  return { success: "パスワードを変更しました" };
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
