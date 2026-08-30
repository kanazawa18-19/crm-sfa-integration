"use client";

import { useActionState } from "react";
import { login } from "@/app/actions";
import BrandLogo from "@/components/BrandLogo";

/**
 * `initialError` は、Googleログインの失敗がServer Actionではなく
 * コールバックからのリダイレクト(`/login?error=...`)で返ってくるため。
 * useSearchParams()を使うとこのページのプリレンダリングがSuspense境界を要求して
 * ビルドが落ちるので、サーバー側で読んで渡している(2026-08-31)。
 */
export default function LoginForm({ initialError }: { initialError?: string }) {
  const [error, formAction, pending] = useActionState(login, undefined);
  const message = error ?? initialError;

  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <form action={formAction} className="surface-card w-full max-w-sm p-8">
        <BrandLogo heightClass="h-8" widthClass="w-36" className="mb-4" />
        <h1 className="page-title mb-6 text-xl">管理画面ログイン</h1>
        <label htmlFor="email" className="mb-1 block text-sm font-medium text-(--color-foreground)/70">
          メールアドレス
        </label>
        <input id="email" name="email" type="email" required autoFocus className="input mb-4 w-full" />
        <label htmlFor="password" className="mb-1 block text-sm font-medium text-(--color-foreground)/70">
          パスワード
        </label>
        <input id="password" name="password" type="password" required className="input mb-4 w-full" />
        {message && <p className="mb-4 text-sm text-(--brand-danger)">{message}</p>}
        <button type="submit" disabled={pending} className="btn-primary w-full">
          {pending ? "確認中..." : "ログイン"}
        </button>
        <div className="my-5 flex items-center gap-3">
          <span className="h-px flex-1 bg-(--color-foreground)/15" />
          <span className="text-xs text-(--color-foreground)/50">または</span>
          <span className="h-px flex-1 bg-(--color-foreground)/15" />
        </div>
        <a href="/login/google/start" className="btn-ghost block w-full text-center">
          Googleでログイン
        </a>
        <a href="/forgot-password" className="link mt-4 block text-center text-xs">
          パスワードをお忘れの方
        </a>
      </form>
    </div>
  );
}
