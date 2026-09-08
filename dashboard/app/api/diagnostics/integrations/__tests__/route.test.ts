import { beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser } from "@/lib/auth";
import { getIntegrationDiagnostic } from "@/lib/backend";
import { POST } from "../route";
vi.mock("@/lib/auth", () => ({ getCurrentUser: vi.fn() }));
vi.mock("@/lib/backend", () => ({ getIntegrationDiagnostic: vi.fn() }));
const request = (target: unknown) => new Request("http://localhost/api/diagnostics/integrations", { method: "POST", headers: { origin: "http://localhost", host: "localhost" }, body: JSON.stringify({ target }) });
describe("診断API", () => {
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(getCurrentUser).mockResolvedValue({ id: "1", role: "master", email: "test@example.invalid", name: null, avatarUrl: null }); });
  it.each([null, "viewer", "editor"] as const)("管理者以外を拒否 %s", async (role) => {
    vi.mocked(getCurrentUser).mockResolvedValue(role ? { id: "1", role, email: "test@example.invalid", name: null, avatarUrl: null } : null);
    expect((await POST(request("slack"))).status).toBe(role ? 403 : 401);
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it.each(["all", "slack,notion", "https://evil.invalid", "", null, ["slack"]])("未知対象・複数対象を拒否 %j", async (target) => {
    expect((await POST(request(target))).status).toBe(400);
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it("認証DB障害でも例外を漏らさない", async () => {
    vi.mocked(getCurrentUser).mockRejectedValue(new Error("SECRET"));
    const response = await POST(request("slack"));
    expect(response.status).toBe(503);
    expect(await response.text()).not.toContain("SECRET");
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it("Nextの内部URLと公開Hostが異なっても同じ送信元を許可する", async () => {
    const req = request("slack");
    req.headers.set("host", "127.0.0.1:4317");
    req.headers.set("origin", "http://127.0.0.1:4317");
    expect(req.headers.get("host")).toBe("127.0.0.1:4317");
    expect(req.headers.get("origin")).toBe("http://127.0.0.1:4317");
    expect((await POST(req)).status).toBe(200);
    expect(getIntegrationDiagnostic).toHaveBeenCalledWith("slack");
  });
  it("偽の転送Hostを送っても他サイトを許可しない", async () => {
    const req = request("slack");
    req.headers.set("host", "app.example.invalid");
    req.headers.set("x-forwarded-host", "evil.invalid");
    req.headers.set("origin", "https://evil.invalid");
    expect((await POST(req)).status).toBe(403);
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it.each([undefined, "cross-site", "same-site"])("Origin欠如を拒否（%s）", async (site) => {
    const req = request("slack"); req.headers.delete("origin");
    if (site) req.headers.set("sec-fetch-site", site);
    expect((await POST(req)).status).toBe(403);
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it.each(["cross-site", "same-site"])("Originが一致していても%sを拒否", async (site) => {
    const req = request("slack"); req.headers.set("sec-fetch-site", site);
    expect((await POST(req)).status).toBe(403);
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it("他サイトからの実行を拒否", async () => {
    const req = request("slack"); req.headers.set("origin", "https://evil.invalid");
    expect((await POST(req)).status).toBe(403);
    expect(getIntegrationDiagnostic).not.toHaveBeenCalled();
  });
  it("許可対象だけ診断し秘密を含まない結果を返す", async () => {
    vi.mocked(getIntegrationDiagnostic).mockResolvedValue({ results: [{ name: "slack", status: "failed", elapsed_ms: 1, detail: "SECRET" }] });
    const response = await POST(request("slack"));
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(getIntegrationDiagnostic).toHaveBeenCalledWith("slack");
    expect(await response.text()).not.toContain("SECRET");
  });
});
