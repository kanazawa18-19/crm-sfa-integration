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
    return <p className="text-sm text-(--brand-danger)">リンクが不正です。メールのリンクからもう一度お試しください。</p>;
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
        className="input"
      />
      <button type="submit" disabled={pending} className="btn-primary">
        {pending ? "設定中..." : "パスワードを設定してログイン画面へ"}
      </button>
      {error && <p className="text-sm text-(--brand-danger)">{error}</p>}
    </form>
  );
}

export default function SetPasswordPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <div className="surface-card w-full max-w-sm p-8">
        <h1 className="page-title mb-2 text-xl">パスワードを設定</h1>
        <p className="mb-4 text-sm text-(--color-foreground)/60">8文字以上の新しいパスワードを入力してください。</p>
        <Suspense fallback={null}>
          <SetPasswordForm />
        </Suspense>
      </div>
    </div>
  );
}
