"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { logout } from "@/app/actions";

// web-engagement-tool(MA)のAdminNav.tsxのサイドバーUXを移植(2026-08-15)。ただし
// ダッシュボード側はナビ項目が少なくグルーピングの必要が無いため、折りたたみ
// グループやテーマ切替は持たない簡略版とする。

type NavLink = { href: string; label: string; exact?: boolean };

const NAV_LINKS: NavLink[] = [
  { href: "/", label: "ダッシュボード", exact: true },
  { href: "/alerts", label: "マネージャー通知" },
  { href: "/reports", label: "日報" },
  { href: "/members", label: "メンバー実績" },
  { href: "/tasks", label: "タスク" },
  { href: "/documents", label: "書類作成" },
  { href: "/settings", label: "設定" },
];

const MASTER_ONLY_NAV_LINKS: NavLink[] = [
  { href: "/users", label: "ユーザー管理" },
  { href: "/settings/security", label: "セキュリティ設定" },
];

const ROLE_LABELS: Record<string, string> = {
  master: "管理者",
  editor: "編集者",
  viewer: "閲覧者",
};

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
      className={`rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
        active ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
      }`}
    >
      {link.label}
    </Link>
  );
}

export default function Sidebar({ role, email }: { role: "master" | "editor" | "viewer"; email: string }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  const links = role === "master" ? [...NAV_LINKS, ...MASTER_ONLY_NAV_LINKS] : NAV_LINKS;
  // Closing the drawer on link click (rather than reacting to pathname changes
  // in an effect) avoids the cascading-render anti-pattern flagged by
  // react-hooks/set-state-in-effect. Harmless to pass on the desktop sidebar
  // too — `open` is already false there.
  const closeDrawer = () => setOpen(false);

  const navBody = (
    <>
      <Link href="/" className="block px-5 py-5 text-lg font-bold text-gray-900">
        営業管理ダッシュボード
      </Link>
      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3">
        {links.map((link) => (
          <NavLinkItem key={link.href} link={link} pathname={pathname} onNavigate={closeDrawer} />
        ))}
      </nav>
      <div className="border-t border-gray-200 px-4 py-3">
        <p className="truncate text-xs font-medium text-gray-700">{email}</p>
        <span className="mt-1 inline-block rounded bg-blue-100 px-2 py-0.5 text-[10px] text-blue-800">
          {ROLE_LABELS[role]}
        </span>
      </div>
      <form action={logout} className="px-3 pb-5">
        <button className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100">
          ログアウト
        </button>
      </form>
    </>
  );

  return (
    <>
      {/* Mobile top bar — hidden on desktop */}
      <header className="sticky top-0 z-40 flex items-center justify-between border-b border-gray-200 bg-white/90 px-3 py-2 backdrop-blur-md md:hidden">
        <Link href="/" className="text-sm font-bold text-gray-900">
          営業管理ダッシュボード
        </Link>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="メニューを開く"
          className="rounded-lg p-2 text-gray-600 hover:bg-gray-100"
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
        className={`fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] flex-col border-r border-gray-200 bg-white transition-transform duration-200 md:hidden ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label="メニューを閉じる"
          className="ml-auto mr-3 mt-3 rounded-lg p-2 text-gray-500 hover:bg-gray-100"
        >
          ✕
        </button>
        {navBody}
      </aside>

      {/* Desktop sidebar — always visible, hidden on mobile */}
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-gray-200 bg-white/80 backdrop-blur-md md:flex">
        {navBody}
      </aside>
    </>
  );
}
