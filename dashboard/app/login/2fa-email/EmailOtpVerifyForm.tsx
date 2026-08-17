"use client";

import { useActionState } from "react";
import { verifyEmailOtpLogin, resendEmailOtpCode } from "@/app/actions";

export default function EmailOtpVerifyForm() {
  const [error, formAction, pending] = useActionState(verifyEmailOtpLogin, undefined);
  const [resendError, resendAction, resendPending] = useActionState(resendEmailOtpCode, undefined);

  return (
    <>
      <form action={formAction} className="flex flex-col gap-3">
        <input
          type="text"
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="123456"
          required
          autoFocus
          className="input text-center text-lg tracking-widest"
        />
        <button type="submit" disabled={pending} className="btn-primary">
          {pending ? "確認中..." : "確認する"}
        </button>
        {error && <p className="text-xs text-(--brand-danger)">{error}</p>}
      </form>
      <form action={resendAction} className="mt-3 text-center">
        <button
          type="submit"
          disabled={resendPending}
          className="link text-xs disabled:cursor-not-allowed disabled:opacity-40"
        >
          {resendPending ? "再送中..." : "コードを再送する"}
        </button>
        {resendError && <p className="mt-1 text-xs text-(--brand-danger)">{resendError}</p>}
      </form>
    </>
  );
}
