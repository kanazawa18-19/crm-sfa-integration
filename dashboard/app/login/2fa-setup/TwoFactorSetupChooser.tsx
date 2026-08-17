"use client";

import { useActionState, useState } from "react";
import { chooseEmailOtpMethod } from "@/app/actions";
import TotpSetupForm from "./TotpSetupForm";

export default function TwoFactorSetupChooser({
  secret,
  qrCodeDataUrl,
  email,
}: {
  secret: string;
  qrCodeDataUrl: string;
  email: string;
}) {
  const [method, setMethod] = useState<"choice" | "totp">("choice");
  const [, formAction, pending] = useActionState(chooseEmailOtpMethod, undefined);

  if (method === "totp") {
    return <TotpSetupForm secret={secret} qrCodeDataUrl={qrCodeDataUrl} email={email} />;
  }

  return (
    <div className="flex flex-col gap-3">
      <button type="button" className="btn-primary" onClick={() => setMethod("totp")}>
        認証アプリで設定する
      </button>
      <form action={formAction}>
        <button type="submit" disabled={pending} className="btn-ghost w-full">
          {pending ? "送信中..." : "メールで受け取る"}
        </button>
      </form>
    </div>
  );
}
