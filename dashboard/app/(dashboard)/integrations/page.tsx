import { requireRole } from "@/lib/auth";
import IntegrationDiagnostics from "./IntegrationDiagnostics";

export default async function IntegrationsPage() {
  await requireRole("master");
  return <IntegrationDiagnostics />;
}
