import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import { verifyPending2FAToken, PENDING_2FA_COOKIE_NAME } from "@/lib/adminSession";
import TotpVerifyForm from "./TotpVerifyForm";

export default async function TotpVerifyPage() {
  const cookieStore = await cookies();
  const pending = verifyPending2FAToken(cookieStore.get(PENDING_2FA_COOKIE_NAME)?.value);
  if (!pending) redirect("/login");

  const user = await prisma.user.findUnique({ where: { id: pending.userId } });
  if (!user) redirect("/login");
  if (!user.totpEnabled) redirect("/login/2fa-setup");

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
        <h1 className="mb-2 text-xl font-bold text-gray-900">2要素認証</h1>
        <p className="mb-4 text-sm text-gray-500">
          認証アプリに表示されている6桁のコード、またはバックアップコードを入力してください。
        </p>
        <TotpVerifyForm />
      </div>
    </div>
  );
}
