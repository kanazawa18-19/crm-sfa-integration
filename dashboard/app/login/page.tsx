"use client";

import { useActionState } from "react";
import { login } from "@/app/actions";
import BrandLogo from "@/components/BrandLogo";

export const dynamic = "force-dynamic";

export default function LoginPage() {
  const [error, formAction, pending] = useActionState(login, undefined);

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
        {error && <p className="mb-4 text-sm text-(--brand-danger)">{error}</p>}
        <button type="submit" disabled={pending} className="btn-primary w-full">
          {pending ? "確認中..." : "ログイン"}
        </button>
        <a href="/forgot-password" className="link mt-4 block text-center text-xs">
          パスワードをお忘れの方
        </a>
      </form>
    </div>
  );
}
