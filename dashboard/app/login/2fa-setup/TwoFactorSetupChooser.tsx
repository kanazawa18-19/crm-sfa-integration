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
      <button
        type="button"
        className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        onClick={() => setMethod("totp")}
      >
        認証アプリで設定する
      </button>
      <form action={chooseEmailOtpMethod}>
        <button
          type="submit"
          className="w-full rounded border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          メールで受け取る
        </button>
      </form>
    </div>
  );
}
