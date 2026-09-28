import { beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser } from "@/lib/auth";
import { resumeProductHold } from "@/lib/backend";
import { POST } from "../route";
vi.mock("@/lib/auth", () => ({ getCurrentUser: vi.fn() }));
vi.mock("@/lib/backend", () => ({ resumeProductHold: vi.fn(), BackendApiError: class extends Error {} }));
const request = (extra = {}, origin = "https://app.example.invalid") => new Request("http://localhost/api/sync-operations", {
  method: "POST", headers: { "Content-Type": "application/json", origin, host: "app.example.invalid" },
  body: JSON.stringify({ project_id: "project", expected_hash: "a".repeat(64), ...extra }),
});
describe("同期の個別承認", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getCurrentUser).mockResolvedValue({ id: "session-manager", name: null, email: "test@example.invalid", role: "viewer", avatarUrl: null, isManager: true });
    vi.mocked(resumeProductHold).mockResolvedValue({ state: "confirmed" });
  });
  it("クライアントが指定した実行者を使わない", async () => {
    expect((await POST(request({ actor_id: "forged-admin" }))).status).toBe(200);
    expect(resumeProductHold).toHaveBeenCalledWith({ project_id: "project", expected_hash: "a".repeat(64), actor_id: "session-manager" });
  });
  it("masterでもmanagerでなければ拒否する", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue({ id: "admin", name: null, email: "test@example.invalid", role: "master", avatarUrl: null, isManager: false });
    expect((await POST(request())).status).toBe(403);
    expect(resumeProductHold).not.toHaveBeenCalled();
  });
  it("他サイトから処理を起動できない", async () => {
    expect((await POST(request({}, "https://evil.invalid"))).status).toBe(403);
    expect(resumeProductHold).not.toHaveBeenCalled();
  });
  it("未ログインなら拒否する", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue(null);
    expect((await POST(request())).status).toBe(401);
  });
});
