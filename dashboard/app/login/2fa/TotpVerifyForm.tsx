"use client";

import { useActionState } from "react";
import { verifyTotpLogin } from "@/app/actions";

export default function TotpVerifyForm() {
  const [error, formAction, pending] = useActionState(verifyTotpLogin, undefined);

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <input
        type="text"
        name="code"
        inputMode="numeric"
        autoComplete="one-time-code"
        placeholder="123456 または バックアップコード"
        required
        autoFocus
        className="input text-center text-lg tracking-widest"
      />
      <button type="submit" disabled={pending} className="btn-primary">
        {pending ? "確認中..." : "確認する"}
      </button>
      {error && <p className="text-xs text-(--brand-danger)">{error}</p>}
    </form>
  );
}
