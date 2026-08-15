"use client";

import { Suspense, useActionState } from "react";
import { useSearchParams } from "next/navigation";
import { confirmEmailChange } from "@/app/actions";

export const dynamic = "force-dynamic";

function ConfirmEmailChangeForm() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [error, formAction, pending] = useActionState(confirmEmailChange, undefined);

  if (!token) {
    return <p className="text-sm text-(--brand-danger)">リンクが不正です。メールのリンクからもう一度お試しください。</p>;
  }

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <input type="hidden" name="token" value={token} />
      <button type="submit" disabled={pending} className="btn-primary">
        {pending ? "確定中..." : "メールアドレスの変更を確定する"}
      </button>
      {error && <p className="text-sm text-(--brand-danger)">{error}</p>}
    </form>
  );
}

export default function ConfirmEmailChangePage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <div className="surface-card w-full max-w-sm p-8">
        <h1 className="page-title mb-2 text-xl">メールアドレス変更の確認</h1>
        <p className="mb-4 text-sm text-(--color-foreground)/60">
          下のボタンを押すと、このアドレスへのメールアドレス変更が確定します。
        </p>
        <Suspense fallback={null}>
          <ConfirmEmailChangeForm />
        </Suspense>
      </div>
    </div>
  );
}
