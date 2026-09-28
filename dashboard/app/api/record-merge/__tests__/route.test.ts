import { beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser } from "@/lib/auth";
import { recordMergeOperation } from "@/lib/backend";
import { POST } from "../route";
vi.mock("@/lib/auth", () => ({ getCurrentUser: vi.fn() }));
vi.mock("@/lib/backend", () => ({ recordMergeOperation: vi.fn(), BackendApiError: class extends Error {} }));
const request = (extra = {}, origin = "https://app.example.invalid") => new Request("http://localhost/api/record-merge", {
  method: "POST", headers: { "Content-Type": "application/json", origin, host: "app.example.invalid" },
  body: JSON.stringify({ action: "list", ...extra }),
});
describe("統合の個別承認", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getCurrentUser).mockResolvedValue({ id: "session-manager", name: null, email: "test@example.invalid", role: "viewer", avatarUrl: null, isManager: true });
    vi.mocked(recordMergeOperation).mockResolvedValue({ state: "confirmed" });
  });
  it.each(["list", "compare", "prepare", "approve", "dismiss", "resume", "compare_alias", "keep_canonical", "compare_relation", "approve_relation", "resume_relation", "compare_creation", "dismiss_creation", "import_creation", "preview_abandon", "abandon", "compare_import_recovery", "recover_import", "preview_reset_import", "reset_import"])("%s をセッション実行者でバックエンドへ渡す", async action => {
    expect((await POST(request({action, actor_id: "forged"}))).status).toBe(200);
    expect(recordMergeOperation).toHaveBeenCalledWith({action, actor_id: "session-manager"});
  });
  it("クライアントが指定した実行者を使わない", async () => {
    expect((await POST(request({ actor_id: "forged-admin" }))).status).toBe(200);
    expect(recordMergeOperation).toHaveBeenCalledWith({ action: "list", actor_id: "session-manager" });
  });
  it("masterでもmanagerでなければ拒否する", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue({ id: "admin", name: null, email: "test@example.invalid", role: "master", avatarUrl: null, isManager: false });
    expect((await POST(request())).status).toBe(403);
    expect(recordMergeOperation).not.toHaveBeenCalled();
  });
  it("他サイトから処理を起動できない", async () => {
    expect((await POST(request({}, "https://evil.invalid"))).status).toBe(403);
    expect(recordMergeOperation).not.toHaveBeenCalled();
  });
  it("未ログインなら拒否する", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue(null);
    expect((await POST(request())).status).toBe(401);
  });
});
