"use client";

import { useActionState, useRef } from "react";
import { changeOwnPassword } from "@/app/actions";

export default function PasswordForm() {
  const formRef = useRef<HTMLFormElement>(null);
  const [state, formAction, pending] = useActionState(async (
    _prevState: { error?: string; success?: string } | undefined,
    formData: FormData
  ) => {
    const result = await changeOwnPassword(_prevState, formData);
    if (result.success) {
      formRef.current?.reset();
    }
    return result;
  }, undefined);

  return (
    <form ref={formRef} action={formAction} className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        現在のパスワード
        <input type="password" name="currentPassword" required autoComplete="current-password" className="input" />
      </label>
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        新しいパスワード(8文字以上)
        <input
          type="password"
          name="newPassword"
          required
          minLength={8}
          autoComplete="new-password"
          className="input"
        />
      </label>
      <div>
        <button type="submit" disabled={pending} className="btn-primary">
          {pending ? "変更中..." : "パスワードを変更"}
        </button>
      </div>
      {state?.error && <p className="text-sm text-(--brand-danger)">{state.error}</p>}
      {state?.success && <p className="text-sm text-(--brand-blue-dark)">{state.success}</p>}
    </form>
  );
}
