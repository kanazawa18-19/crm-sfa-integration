"use client";

import { Suspense, useActionState } from "react";
import { useSearchParams } from "next/navigation";
import { setPassword } from "@/app/actions";

export const dynamic = "force-dynamic";

function SetPasswordForm() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [error, formAction, pending] = useActionState(setPassword, undefined);

  if (!token) {
    return <p className="text-sm text-red-600">リンクが不正です。メールのリンクからもう一度お試しください。</p>;
  }

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <input type="hidden" name="token" value={token} />
      <input
        type="password"
        name="password"
        placeholder="新しいパスワード(8文字以上)"
        required
        minLength={8}
        autoFocus
        className="rounded border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {pending ? "設定中..." : "パスワードを設定してログイン画面へ"}
      </button>
      {error && <p className="text-sm text-red-600">{error}</p>}
    </form>
  );
}

export default function SetPasswordPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
        <h1 className="mb-2 text-xl font-bold text-gray-900">パスワードを設定</h1>
        <p className="mb-4 text-sm text-gray-500">8文字以上の新しいパスワードを入力してください。</p>
        <Suspense fallback={null}>
          <SetPasswordForm />
        </Suspense>
      </div>
    </div>
  );
}
