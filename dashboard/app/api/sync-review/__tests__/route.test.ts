import { beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser } from "@/lib/auth";
import { decideSyncFieldReview } from "@/lib/backend";
import { POST } from "../route";
vi.mock("@/lib/auth", () => ({ getCurrentUser: vi.fn() }));
vi.mock("@/lib/backend", () => ({ decideSyncFieldReview: vi.fn(), BackendApiError: class extends Error {} }));
const request = (extra = {}, origin = "https://app.example.invalid") => new Request("http://localhost/api/sync-review", {
  method: "POST", headers: { "Content-Type": "application/json", origin, host: "app.example.invalid" },
  body: JSON.stringify({ id: "review", action: "confirm", revision: 0, ...extra }),
});
describe("同期の個別承認", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getCurrentUser).mockResolvedValue({ id: "session-manager", name: null, email: "test@example.invalid", role: "viewer", avatarUrl: null, isManager: true });
    vi.mocked(decideSyncFieldReview).mockResolvedValue({ state: "confirmed" });
  });
  it("クライアントが指定した実行者を使わない", async () => {
    expect((await POST(request({ actor_id: "forged-admin" }))).status).toBe(200);
    expect(decideSyncFieldReview).toHaveBeenCalledWith({ id: "review", action: "confirm", revision: 0, restore_from: undefined, actor_id: "session-manager" });
  });
  it("masterでもmanagerでなければ拒否する", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue({ id: "admin", name: null, email: "test@example.invalid", role: "master", avatarUrl: null, isManager: false });
    expect((await POST(request())).status).toBe(403);
    expect(decideSyncFieldReview).not.toHaveBeenCalled();
  });
  it("他サイトから処理を起動できない", async () => {
    expect((await POST(request({}, "https://evil.invalid"))).status).toBe(403);
    expect(decideSyncFieldReview).not.toHaveBeenCalled();
  });
  it("未ログインなら拒否する", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue(null);
    expect((await POST(request())).status).toBe(401);
  });
});
