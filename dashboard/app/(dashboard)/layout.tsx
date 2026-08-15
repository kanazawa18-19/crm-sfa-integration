import Sidebar from "@/components/Sidebar";
import { getCurrentUser } from "@/lib/auth";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const user = await getCurrentUser();

  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <Sidebar role={user?.role ?? "viewer"} email={user?.email ?? ""} />
      <main className="min-w-0 flex-1 px-6 py-8">{children}</main>
    </div>
  );
}
