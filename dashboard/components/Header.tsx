import Link from "next/link";

const navItems = [
  { href: "/", label: "ダッシュボード" },
  { href: "/alerts", label: "マネージャー通知" },
  { href: "/reports", label: "日報" },
  { href: "/members", label: "メンバー実績" },
  { href: "/tasks", label: "タスク" },
  { href: "/documents", label: "書類作成" },
  { href: "/settings", label: "設定" },
];

const MASTER_ONLY_NAV_ITEMS = [
  { href: "/users", label: "ユーザー管理" },
  { href: "/settings/security", label: "セキュリティ設定" },
];

export default function Header({ role }: { role: "master" | "editor" | "viewer" }) {
  const items = role === "master" ? [...navItems, ...MASTER_ONLY_NAV_ITEMS] : navItems;

  return (
    <header className="border-b border-gray-200 bg-white">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <span className="text-lg font-bold text-gray-900">営業管理ダッシュボード</span>
        <nav className="flex flex-wrap gap-6">
          {items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="text-sm font-medium text-gray-600 hover:text-gray-900"
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
