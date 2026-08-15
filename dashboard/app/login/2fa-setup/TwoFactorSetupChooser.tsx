"use client";

import { useState } from "react";
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

  if (method === "totp") {
    return <TotpSetupForm secret={secret} qrCodeDataUrl={qrCodeDataUrl} email={email} />;
  }

  return (
    <div className="flex flex-col gap-3">
      <button type="button" className="btn-primary" onClick={() => setMethod("totp")}>
        認証アプリで設定する
      </button>
      <form action={chooseEmailOtpMethod}>
        <button type="submit" className="btn-ghost w-full">
          メールで受け取る
        </button>
      </form>
    </div>
  );
}
