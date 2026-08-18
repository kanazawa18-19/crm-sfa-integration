"use client";

import { useActionState } from "react";
import { updateOwnProfile } from "@/app/actions";

export default function NameForm({
  initialName,
  initialTitle,
  initialDepartment,
}: {
  initialName: string;
  initialTitle: string;
  initialDepartment: string;
}) {
  const [state, formAction, pending] = useActionState(updateOwnProfile, undefined);

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        表示名(任意)
        <input
          type="text"
          name="name"
          defaultValue={initialName}
          maxLength={100}
          placeholder="山田 太郎"
          className="input"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        役職(任意)
        <input
          type="text"
          name="title"
          defaultValue={initialTitle}
          maxLength={100}
          placeholder="マーケティング本部長"
          className="input"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm text-(--color-foreground)">
        部署(任意)
        <input
          type="text"
          name="department"
          defaultValue={initialDepartment}
          maxLength={100}
          placeholder="営業部"
          className="input"
        />
      </label>
      <div>
        <button type="submit" disabled={pending} className="btn-primary">
          {pending ? "保存中..." : "保存"}
        </button>
      </div>
      {state?.error && <p className="text-sm text-(--brand-danger)">{state.error}</p>}
      {state?.success && <p className="text-sm text-(--brand-blue-dark)">{state.success}</p>}
    </form>
  );
}
