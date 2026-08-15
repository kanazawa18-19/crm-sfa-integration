"use client";

import { useActionState } from "react";
import { requestPasswordReset } from "@/app/actions";

export default function ForgotPasswordPage() {
  const [message, formAction, pending] = useActionState(requestPasswordReset, undefined);

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
        <h1 className="mb-2 text-xl font-bold text-gray-900">パスワード再設定</h1>
        <p className="mb-4 text-sm text-gray-500">
          登録済みのメールアドレスを入力してください。再設定用のリンクをお送りします。
        </p>

        <form action={formAction} className="flex flex-col gap-3">
          <input
            type="email"
            name="email"
            placeholder="メールアドレス"
            required
            className="rounded border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
          />
          <button
            type="submit"
            disabled={pending}
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {pending ? "送信中..." : "再設定リンクを送信"}
          </button>
          {message && <p className="text-sm text-blue-700">{message}</p>}
        </form>
        <a href="/login" className="mt-4 block text-xs text-blue-600 underline">
          ← ログイン画面に戻る
        </a>
      </div>
    </div>
  );
}
