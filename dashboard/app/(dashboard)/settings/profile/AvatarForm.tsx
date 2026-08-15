"use client";

import { useActionState, useState } from "react";
import { updateOwnAvatar } from "@/app/actions";
import { AVATAR_MAX_BYTES, validateAvatarFile } from "@/lib/avatar";

export default function AvatarForm({ initialAvatarUrl }: { initialAvatarUrl: string | null }) {
  const [state, formAction, pending] = useActionState(updateOwnAvatar, undefined);
  const [previewUrl, setPreviewUrl] = useState<string | null>(initialAvatarUrl);
  const [clientError, setClientError] = useState<string | null>(null);

  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;

    const error = validateAvatarFile(file);
    setClientError(error);
    if (error) return;

    setPreviewUrl(URL.createObjectURL(file));
  }

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <div className="flex items-center gap-4">
        {previewUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={previewUrl}
            alt="アイコンプレビュー"
            className="h-16 w-16 rounded-full object-cover border border-(--border-subtle)"
          />
        ) : (
          <div className="h-16 w-16 rounded-full bg-(--color-surface-muted)" aria-hidden="true" />
        )}
        <input
          type="file"
          name="avatar"
          accept="image/png,image/jpeg,image/webp"
          onChange={handleFileChange}
          className="text-sm"
        />
      </div>
      <p className="text-xs text-(--color-foreground)/50">
        png / jpeg / webp、{Math.floor(AVATAR_MAX_BYTES / 1024 / 1024)}MB以内。
      </p>
      <div>
        <button type="submit" disabled={pending || !!clientError} className="btn-primary">
          {pending ? "アップロード中..." : "アイコンを更新"}
        </button>
      </div>
      {clientError && <p className="text-sm text-(--brand-danger)">{clientError}</p>}
      {!clientError && state?.error && <p className="text-sm text-(--brand-danger)">{state.error}</p>}
      {state?.success && <p className="text-sm text-(--brand-blue-dark)">{state.success}</p>}
    </form>
  );
}
