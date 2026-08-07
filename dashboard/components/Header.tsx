import Link from "next/link";

const navItems = [
  { href: "/", label: "ダッシュボード" },
  { href: "/reports", label: "日報" },
  { href: "/members", label: "メンバー実績" },
  { href: "/tasks", label: "タスク" },
  { href: "/documents", label: "書類作成" },
];

export default function Header() {
  return (
    <header className="border-b border-gray-200 bg-white">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <span className="text-lg font-bold text-gray-900">営業管理ダッシュボード</span>
        <nav className="flex gap-6">
          {navItems.map((item) => (
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
