"use client";

import { useActionState } from "react";
import { updateOwnName } from "@/app/actions";

export default function NameForm({ initialName }: { initialName: string }) {
  const [state, formAction, pending] = useActionState(updateOwnName, undefined);

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        表示名
        <input
          type="text"
          name="name"
          defaultValue={initialName}
          maxLength={100}
          placeholder="山田 太郎"
          className="input"
        />
      </label>
      <div>
        <button type="submit" disabled={pending} className="btn-primary">
          {pending ? "保存中..." : "表示名を保存"}
        </button>
      </div>
      {state?.error && <p className="text-sm text-(--brand-danger)">{state.error}</p>}
      {state?.success && <p className="text-sm text-(--brand-blue-dark)">{state.success}</p>}
    </form>
  );
}
