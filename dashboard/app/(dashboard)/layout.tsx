import Sidebar from "@/components/Sidebar";
import AvatarMenu from "@/components/AvatarMenu";
import { getCurrentUser } from "@/lib/auth";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const user = await getCurrentUser();

  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <Sidebar role={user?.role ?? "viewer"} email={user?.email ?? ""} />
      <div className="flex min-w-0 flex-1 flex-col">
        {user && (
          <header className="sticky top-0 z-30 hidden items-center justify-end border-b border-(--border-subtle) bg-(--color-surface)/90 px-6 py-3 backdrop-blur-md md:flex">
            <AvatarMenu user={user} />
          </header>
        )}
        <main className="min-w-0 flex-1 px-6 py-8">{children}</main>
      </div>
    </div>
  );
}
