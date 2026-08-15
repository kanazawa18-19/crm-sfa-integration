"use client";

import { useActionState } from "react";
import { requestPasswordReset } from "@/app/actions";

export default function ForgotPasswordPage() {
  const [message, formAction, pending] = useActionState(requestPasswordReset, undefined);

  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <div className="surface-card w-full max-w-sm p-8">
        <h1 className="page-title mb-2 text-xl">パスワード再設定</h1>
        <p className="mb-4 text-sm text-(--color-foreground)/60">
          登録済みのメールアドレスを入力してください。再設定用のリンクをお送りします。
        </p>

        <form action={formAction} className="flex flex-col gap-3">
          <input type="email" name="email" placeholder="メールアドレス" required className="input" />
          <button type="submit" disabled={pending} className="btn-primary">
            {pending ? "送信中..." : "再設定リンクを送信"}
          </button>
          {message && <p className="text-sm text-(--brand-blue-dark)">{message}</p>}
        </form>
        <a href="/login" className="link mt-4 inline-block text-xs">
          ← ログイン画面に戻る
        </a>
      </div>
    </div>
  );
}
