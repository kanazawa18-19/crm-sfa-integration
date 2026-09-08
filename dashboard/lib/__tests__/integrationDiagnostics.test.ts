import { beforeEach, describe, expect, it, vi } from "vitest";
import { getIntegrationDiagnostic } from "@/lib/backend";
import { runSafeDiagnostic, sanitizeDiagnostic } from "@/lib/integrationDiagnosticsServer";
vi.mock("@/lib/backend", () => ({ getIntegrationDiagnostic: vi.fn() }));
const payload = (name: string, extra?: unknown, status = "ok") => ({ results: [{ name, status, elapsed_ms: 12, detail: "SECRET 顧客メール", extra }] });
describe("連携診断の安全な表示", () => {
  beforeEach(() => vi.resetAllMocks());
  it("生の詳細・追加情報を返さない", () => {
    const result = sanitizeDiagnostic("notion", payload("notion", { token: "SECRET", customer: "顧客メール" }), 20);
    expect(result.status).toBe("ok");
    expect(JSON.stringify(result)).not.toMatch(/SECRET|顧客メール|token/);
  });
  it.each([{ results: [{ name: "notion", status: ["ok"], elapsed_ms: 0 }] }, {}, { results: [] }, { results: [{ name: "slack", status: "ok", elapsed_ms: 0 }] }, { results: [{ name: "notion", status: "other", elapsed_ms: 0 }] }, { results: [{ name: "notion", status: "ok" }] }])("不完全な結果を正常にしない %j", (value) => {
    expect(() => sanitizeDiagnostic("notion", value, 1)).toThrow();
  });
  it("無効時も各DBの設定を表示し未知のキーを捨てる", () => {
    const result = sanitizeDiagnostic("spreadsheet_row_creation", payload("spreadsheet_row_creation", { per_db: { client_master: false, chain: false, contact: false, project: false, product: false, action: false, SECRET: true }, unknown_db_keys_count: 1, wildcard: false }, "not_configured"), 1);
    expect(result.status).toBe("not_configured");
    expect(result.facts).toHaveLength(8);
    expect(JSON.stringify(result)).not.toContain("SECRET");
  });
  it("受信は固定ソースの件数と正規化日時だけを出す", () => {
    const result = sanitizeDiagnostic("webhook_receipts", payload("webhook_receipts", { sources: { notion: { count: 3, last_received_at: "2026-09-09T00:00:00Z", hours_since: 1, secret: "SECRET" }, SECRET: {} } }), 1);
    expect(result.facts).toHaveLength(4);
    expect(JSON.stringify(result)).not.toContain("SECRET");
  });
  it.each(["SECRET", -1, 1.5, null])("不正な集計値は未確認に倒す %s", (value) => {
    expect(() => sanitizeDiagnostic("spreadsheet_outbox", payload("spreadsheet_outbox", { by_status: { pending: { count: value } }, pending_oldest_age_days: 1 }), 1)).toThrow();
  });
  it("GROUP BYの空集計は各状態0件として扱う", () => {
    const result = sanitizeDiagnostic("spreadsheet_outbox", payload("spreadsheet_outbox", { by_status: {}, pending_oldest_age_days: null }), 1);
    expect(result.status).toBe("ok");
    expect(result.facts.map((fact) => fact.value)).toEqual(["0件", "0件", "0件"]);
  });
  it("滞留日数自体の欠損は正常にしない", () => {
    expect(() => sanitizeDiagnostic("spreadsheet_outbox", payload("spreadsheet_outbox", { by_status: {} }), 1)).toThrow();
  });
  it("行作成待ちの集計だけを許可する", () => {
    const result = sanitizeDiagnostic("spreadsheet_outbox", payload("spreadsheet_outbox", { by_status: { pending: { count: 2, last_error: "SECRET" } }, pending_oldest_age_days: 1, items: ["SECRET"] }), 1);
    expect(result.facts[0].value).toBe("2件");
    expect(JSON.stringify(result)).not.toContain("SECRET");
  });
  it("不完全なバックエンド応答は例外500ではなく安全な未確認にする", async () => {
    vi.mocked(getIntegrationDiagnostic).mockResolvedValue({ results: [{ name: "slack", status: "ok", detail: "SECRET" }], detail: "SECRET" });
    const result = await runSafeDiagnostic("slack");
    expect(result.status).toBe("unknown");
    expect(result.facts).toEqual([]);
    expect(JSON.stringify(result)).not.toContain("SECRET");
  });
  it("例外本文と時間切れを固定の未確認に変換する", async () => {
    vi.mocked(getIntegrationDiagnostic).mockRejectedValue(new Error("SECRET"));
    const result = await runSafeDiagnostic("slack");
    expect(result.status).toBe("unknown");
    expect(JSON.stringify(result)).not.toContain("SECRET");
  });
});
