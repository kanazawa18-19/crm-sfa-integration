"use client";

import { useActionState } from "react";
import { login } from "@/app/actions";

export const dynamic = "force-dynamic";

export default function LoginPage() {
  const [error, formAction, pending] = useActionState(login, undefined);

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <form
        action={formAction}
        className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-8 shadow-sm"
      >
        <h1 className="mb-6 text-xl font-bold text-gray-900">管理画面ログイン</h1>
        <label htmlFor="email" className="mb-1 block text-sm font-medium text-gray-700">
          メールアドレス
        </label>
        <input
          id="email"
          name="email"
          type="email"
          required
          autoFocus
          className="mb-4 w-full rounded border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
        />
        <label htmlFor="password" className="mb-1 block text-sm font-medium text-gray-700">
          パスワード
        </label>
        <input
          id="password"
          name="password"
          type="password"
          required
          className="mb-4 w-full rounded border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
        />
        {error && <p className="mb-4 text-sm text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={pending}
          className="w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {pending ? "確認中..." : "ログイン"}
        </button>
        <a href="/forgot-password" className="mt-4 block text-center text-xs text-blue-600 underline">
          パスワードをお忘れの方
        </a>
      </form>
    </div>
  );
}
