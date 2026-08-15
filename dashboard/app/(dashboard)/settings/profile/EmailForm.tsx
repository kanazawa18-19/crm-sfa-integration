"use client";

import { useActionState } from "react";
import { requestOwnEmailChange } from "@/app/actions";

export default function EmailForm({ currentEmail }: { currentEmail: string }) {
  const [state, formAction, pending] = useActionState(requestOwnEmailChange, undefined);

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <p className="text-sm text-(--color-foreground)">
        現在のメールアドレス: <span className="font-medium">{currentEmail}</span>
      </p>
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        新しいメールアドレス
        <input type="email" name="newEmail" required placeholder="new@example.com" className="input" />
      </label>
      <p className="text-xs text-(--color-foreground)/50">
        新しいメールアドレス宛に確認リンクを送信します。リンクを開くまでメールアドレスは変更されません。
      </p>
      <div>
        <button type="submit" disabled={pending} className="btn-primary">
          {pending ? "送信中..." : "確認メールを送信"}
        </button>
      </div>
      {state?.error && <p className="text-sm text-(--brand-danger)">{state.error}</p>}
      {state?.success && <p className="text-sm text-(--brand-blue-dark)">{state.success}</p>}
    </form>
  );
}
