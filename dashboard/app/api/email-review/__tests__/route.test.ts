import { beforeEach, expect, it, vi } from "vitest";
import { getCurrentUser } from "@/lib/auth";
import { emailReviewRequest } from "@/lib/backend";
import { GET, POST } from "../route";
vi.mock("@/lib/auth", () => ({ getCurrentUser: vi.fn() }));
vi.mock("@/lib/backend", () => ({ emailReviewRequest: vi.fn(), BackendApiError: class extends Error {} }));
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getCurrentUser).mockResolvedValue({ id: "member", name: null, email: "member@example.test", role: "viewer", avatarUrl: null });
  vi.mocked(emailReviewRequest).mockResolvedValue({ rows: [] });
});
const post = (origin = "https://app.example.test") => new Request("https://app.example.test/api/email-review", {
  method: "POST", headers: { "Content-Type": "application/json", origin },
  body: JSON.stringify({ id: "r", revision: 0, action: "hold", actor_id: "other" }),
});
it("一般メンバーが本人として判断し、実行者の差替えを許さない", async () => {
  expect((await POST(post())).status).toBe(200);
  expect(emailReviewRequest).toHaveBeenCalledWith("/decision", { id: "r", revision: 0, action: "hold", actor_id: "member" });
});
it("外部サイトからの判断を拒否する", async () => {
  expect((await POST(post("https://evil.example.test"))).status).toBe(403);
  expect(emailReviewRequest).not.toHaveBeenCalled();
});
it("一覧もログイン本人だけを問い合わせる", async () => {
  expect((await GET(new Request("https://app.example.test/api/email-review?actor_id=other"))).status).toBe(200);
  expect(emailReviewRequest).toHaveBeenCalledWith("?actor_id=member&state=pending&offset=0");
});
it("未ログインを拒否する", async () => {
  vi.mocked(getCurrentUser).mockResolvedValue(null);
  expect((await GET(new Request("https://app.example.test/api/email-review"))).status).toBe(401);
});
