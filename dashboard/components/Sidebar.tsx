"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { logout } from "@/app/actions";
import BrandLogo from "@/components/BrandLogo";
import ThemeToggle from "@/components/ThemeToggle";
import SubmitButton from "@/components/SubmitButton";
import { ROLE_LABELS } from "@/lib/roleLabels";

// web-engagement-tool(MA)のAdminNav.tsxのサイドバーUXを移植(2026-08-15)。ただし
// ダッシュボード側はナビ項目が少ないため、見出しによるグルーピングは行うが
// アコーディオン式の折りたたみ機能は省略する。2026-08-17: smartHRトライアル
// 画面のIAパターンに倣い、「個人設定」(プロフィール編集/Gmail連携)は
// デスクトップでは画面右上のAvatarMenu(components/AvatarMenu.tsx)に一本化した。
// ただしAvatarMenuを含むヘッダーはデスクトップ専用(hidden md:flex)なので、
// モバイルドロワーからも撤去すると個人設定への導線が消えてしまう。そのため
// 「個人設定」グループ自体はNAV_GROUPSに残しつつ mobileOnly フラグでモバイル
// ドロワーのみ表示し、デスクトップサイドバーでは非表示にする。
//
// NavGroup.id はサイドバー内部の安定した識別子(表示ラベル変更の影響を受けない)。
// master限定リンクの注入判定にはこの id を用いる。

type NavLink = { href: string; label: string; exact?: boolean };
type NavGroup = { id: string; label: string; links: NavLink[]; mobileOnly?: boolean };

// Pinned outside any group — always one click away.
const HOME_LINK: NavLink = { href: "/", label: "ダッシュボード", exact: true };

const NAV_GROUPS: NavGroup[] = [
  {
    id: "sales",
    label: "営業管理",
    links: [
      { href: "/alerts", label: "マネージャー通知" },
      { href: "/reports", label: "日報" },
      { href: "/members", label: "メンバー実績" },
      { href: "/tasks", label: "タスク" },
      { href: "/documents", label: "書類作成" },
    ],
  },
  {
    id: "personal-settings",
    label: "個人設定",
    mobileOnly: true,
    links: [
      { href: "/settings/profile", label: "プロフィール編集" },
      { href: "/settings/gmail", label: "Gmail連携" },
      { href: "/settings/drive", label: "Drive連携" },
    ],
  },
  {
    id: "org-settings",
    label: "共通設定",
    links: [{ href: "/settings", label: "設定" }],
  },
];

const MASTER_ONLY_NAV_LINKS: NavLink[] = [
  { href: "/users", label: "ユーザー管理" },
  { href: "/audit-log", label: "データ監査ログ" },
  { href: "/settings/security", label: "セキュリティ設定" },
  { href: "/settings/email-reminders", label: "未返信メールリマインド設定" },
  { href: "/settings/document-approvers", label: "見積書承認者管理" },
];

function isLinkActive(link: NavLink, pathname: string | null) {
  return link.exact ? pathname === link.href : Boolean(pathname?.startsWith(link.href));
}

function NavLinkItem({
  link,
  pathname,
  onNavigate,
}: {
  link: NavLink;
  pathname: string | null;
  onNavigate?: () => void;
}) {
  const active = isLinkActive(link, pathname);
  return (
    <Link
      href={link.href}
      onClick={onNavigate}
      className={`rounded-[6px] px-3 py-2 text-sm font-medium transition-colors ${
        active
          ? "bg-(--brand-blue) text-white"
          : "text-(--color-foreground)/70 hover:bg-(--color-surface-muted)"
      }`}
    >
      {link.label}
    </Link>
  );
}

function NavGroupSection({
  group,
  pathname,
  onNavigate,
}: {
  group: NavGroup;
  pathname: string | null;
  onNavigate?: () => void;
}) {
  return (
    <div>
      <p className="px-3 py-1.5 text-[11px] font-semibold tracking-wide text-(--color-foreground)/40 uppercase">
        {group.label}
      </p>
      <div className="flex flex-col gap-0.5 pb-1">
        {group.links.map((link) => (
          <NavLinkItem key={link.href} link={link} pathname={pathname} onNavigate={onNavigate} />
        ))}
      </div>
    </div>
  );
}

export default function Sidebar({ role, email }: { role: "master" | "editor" | "viewer"; email: string }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  const groups =
    role === "master"
      ? NAV_GROUPS.map((g) =>
          g.id === "org-settings" ? { ...g, links: [...g.links, ...MASTER_ONLY_NAV_LINKS] } : g
        )
      : NAV_GROUPS;
  // Closing the drawer on link click (rather than reacting to pathname changes
  // in an effect) avoids the cascading-render anti-pattern flagged by
  // react-hooks/set-state-in-effect. Harmless to pass on the desktop sidebar
  // too — `open` is already false there.
  const closeDrawer = () => setOpen(false);

  const navBody = (
    <>
      <Link href="/" className="flex flex-col items-start gap-1 px-5 py-5">
        <BrandLogo heightClass="h-6" widthClass="w-36" />
        <span className="text-xs font-medium tracking-tight text-(--color-foreground)/50">
          営業管理ダッシュボード
        </span>
      </Link>
      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3">
        <NavLinkItem link={HOME_LINK} pathname={pathname} onNavigate={closeDrawer} />
        {groups.map((group) =>
          group.mobileOnly ? (
            <div key={group.id} className="md:hidden">
              <NavGroupSection group={group} pathname={pathname} onNavigate={closeDrawer} />
            </div>
          ) : (
            <NavGroupSection key={group.id} group={group} pathname={pathname} onNavigate={closeDrawer} />
          )
        )}
      </nav>
      <div className="border-t border-(--border-subtle) px-4 py-3">
        <p className="mb-2 text-[10px] font-semibold text-(--color-foreground)/40">表示テーマ</p>
        <ThemeToggle />
      </div>
      <div className="border-t border-(--border-subtle) px-4 py-3">
        <p className="truncate text-xs font-medium text-(--color-foreground)/80">{email}</p>
        <span className="badge-blue mt-1 inline-block text-[10px]">{ROLE_LABELS[role]}</span>
      </div>
      <form action={logout} className="px-3 pb-5">
        <SubmitButton pendingLabel="ログアウト中..." className="btn-ghost btn-xs w-full">
          ログアウト
        </SubmitButton>
      </form>
    </>
  );

  return (
    <>
      {/* Mobile top bar — hidden on desktop */}
      <header className="sticky top-0 z-40 flex items-center justify-between border-b border-(--border-subtle) bg-(--color-surface)/90 px-3 py-2 backdrop-blur-md md:hidden">
        <Link href="/" className="flex items-center gap-2">
          <BrandLogo heightClass="h-5" widthClass="w-28" />
          <span className="text-xs font-medium tracking-tight text-(--color-foreground)/50">
            営業管理ダッシュボード
          </span>
        </Link>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="メニューを開く"
          className="rounded-lg p-2 text-(--color-foreground)/70 hover:bg-(--color-surface-muted)"
        >
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      {/* Mobile drawer + backdrop */}
      {open && (
        <div className="fixed inset-0 z-50 bg-black/40 md:hidden" onClick={() => setOpen(false)} aria-hidden="true" />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] flex-col border-r border-(--border-subtle) bg-(--color-surface) transition-transform duration-200 md:hidden ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label="メニューを閉じる"
          className="ml-auto mr-3 mt-3 rounded-lg p-2 text-(--color-foreground)/50 hover:bg-(--color-surface-muted)"
        >
          ✕
        </button>
        {navBody}
      </aside>

      {/* Desktop sidebar — always visible, hidden on mobile */}
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-(--border-subtle) bg-(--color-surface)/80 backdrop-blur-md md:flex">
        {navBody}
      </aside>
    </>
  );
}
