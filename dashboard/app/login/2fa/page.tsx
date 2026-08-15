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
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <div className="surface-card w-full max-w-sm p-8">
        <h1 className="page-title mb-2 text-xl">2要素認証</h1>
        <p className="mb-4 text-sm text-(--color-foreground)/60">
          認証アプリに表示されている6桁のコード、またはバックアップコードを入力してください。
        </p>
        <TotpVerifyForm />
      </div>
    </div>
  );
}
