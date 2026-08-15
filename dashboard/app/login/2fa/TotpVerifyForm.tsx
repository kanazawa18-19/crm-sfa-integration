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
        className="rounded border border-gray-300 px-3 py-2 text-center text-lg tracking-widest text-gray-900 focus:border-blue-500 focus:outline-none"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {pending ? "確認中..." : "確認する"}
      </button>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </form>
  );
}
