import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import { verifyPending2FAToken, PENDING_2FA_COOKIE_NAME } from "@/lib/adminSession";
import { generateTotpSecret, generateTotpQrCodeDataUrl } from "@/lib/twoFactor";
import TwoFactorSetupChooser from "./TwoFactorSetupChooser";

export default async function TotpSetupPage() {
  const cookieStore = await cookies();
  const pending = verifyPending2FAToken(cookieStore.get(PENDING_2FA_COOKIE_NAME)?.value);
  if (!pending) redirect("/login");

  const user = await prisma.user.findUnique({ where: { id: pending.userId } });
  if (!user) redirect("/login");
  if (user.totpEnabled) redirect("/login/2fa");
  if (user.emailOtpEnabled) redirect("/login/2fa-email");

  const secret = generateTotpSecret();
  const qrCodeDataUrl = await generateTotpQrCodeDataUrl(user.email, secret);

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
        <h1 className="mb-2 text-xl font-bold text-gray-900">2要素認証の設定</h1>
        <p className="mb-4 text-sm text-gray-500">
          管理者により2要素認証が必須化されました。認証アプリ(Google Authenticator、1Password等)でQRコードを読み取るか、メールでコードを受け取るか選んでください。
        </p>
        <TwoFactorSetupChooser secret={secret} qrCodeDataUrl={qrCodeDataUrl} email={user.email} />
      </div>
    </div>
  );
}
