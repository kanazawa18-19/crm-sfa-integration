"use client";

import { useActionState } from "react";
import { inviteUser } from "@/app/actions";

const ROLE_LABELS: Record<string, string> = {
  viewer: "閲覧者",
  editor: "編集者",
  master: "管理者",
};

export default function InviteUserForm() {
  const [, formAction, pending] = useActionState(inviteUser, undefined);

  return (
    <form action={formAction} className="mt-3 flex flex-wrap gap-2">
      <input name="email" type="email" placeholder="メールアドレス" required className="input" />
      <select name="role" defaultValue="viewer" className="input">
        {Object.entries(ROLE_LABELS).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      <button type="submit" disabled={pending} className="btn-primary">
        {pending ? "送信中..." : "招待を送信"}
      </button>
    </form>
  );
}
