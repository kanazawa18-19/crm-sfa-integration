"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { CurrentUser } from "@/lib/auth";
import { ROLE_LABELS } from "@/lib/roleLabels";

// smartHRトライアル画面のIAに倣い、個人設定(プロフィール編集/Gmail連携)を
// 画面右上のアバタードロップダウンへ集約(2026-08-17)。ログアウトは既存の
// Sidebar側に残す(このメニューでは扱わない)。

function getInitial(name: string | null, email: string) {
  const source = name?.trim() || email;
  return source.charAt(0).toUpperCase() || "?";
}

export default function AvatarMenu({ user }: { user: CurrentUser }) {
  const { email, role, name, avatarUrl } = user;
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;

    function handlePointerDown(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="個人設定メニューを開く"
        className="flex items-center gap-2 rounded-full p-1 pr-3 transition-colors hover:bg-(--color-surface-muted)"
      >
        {avatarUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={avatarUrl}
            alt=""
            className="h-8 w-8 rounded-full border border-(--border-subtle) object-cover"
          />
        ) : (
          <span
            className="flex h-8 w-8 items-center justify-center rounded-full bg-(--brand-blue) text-sm font-semibold text-white"
            aria-hidden="true"
          >
            {getInitial(name, email)}
          </span>
        )}
        <span className="badge-blue text-[10px]">{ROLE_LABELS[role]}</span>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-[calc(100%+8px)] z-40 w-64 rounded-[8px] border border-(--border-subtle) bg-(--color-surface) py-2 shadow-lg"
        >
          <div className="border-b border-(--border-subtle) px-4 pb-2">
            <p className="truncate text-xs font-medium text-(--color-foreground)/80">{email}</p>
          </div>
          <p className="px-4 pt-2 pb-1 text-[11px] font-semibold tracking-wide text-(--color-foreground)/40 uppercase">
            個人設定
          </p>
          <div className="flex flex-col gap-0.5 px-2">
            <Link
              href="/settings/profile"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="rounded-[6px] px-3 py-2 text-sm font-medium text-(--color-foreground)/70 transition-colors hover:bg-(--color-surface-muted)"
            >
              プロフィール編集
            </Link>
            <Link
              href="/settings/gmail"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="rounded-[6px] px-3 py-2 text-sm font-medium text-(--color-foreground)/70 transition-colors hover:bg-(--color-surface-muted)"
            >
              Gmail連携
            </Link>
            <Link
              href="/settings/drive"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="rounded-[6px] px-3 py-2 text-sm font-medium text-(--color-foreground)/70 transition-colors hover:bg-(--color-surface-muted)"
            >
              Drive連携
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
