import { requireRole } from "@/lib/auth";
import ClientsPageClient from "./ClientsPageClient";

export const dynamic = "force-dynamic";

export default async function ClientsPage() {
  await requireRole("viewer");

  return <ClientsPageClient />;
}
