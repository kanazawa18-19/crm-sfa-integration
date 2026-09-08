import { afterEach, expect, it, vi } from "vitest";
import { getIntegrationDiagnostic } from "@/lib/backend";
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
it("診断だけにタイムアウトとリダイレクト拒否を渡す", async () => {
  vi.stubEnv("BACKEND_API_URL", "https://backend.example.invalid");
  vi.stubEnv("BACKEND_API_TOKEN", "test-token");
  const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
  vi.stubGlobal("fetch", fetchMock);
  await getIntegrationDiagnostic("notion");
  expect(fetchMock).toHaveBeenCalledWith("https://backend.example.invalid/api/diagnostics/integrations?only=notion", expect.objectContaining({ redirect: "error", cache: "no-store", signal: expect.any(AbortSignal) }));
});
it("時間切れで取得を終了する", async () => {
  vi.stubEnv("BACKEND_API_URL", "https://backend.example.invalid");
  const aborted = AbortSignal.abort(new DOMException("時間切れ", "TimeoutError"));
  const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(aborted);
  vi.stubGlobal("fetch", vi.fn((_url, options) => { options.signal.throwIfAborted(); }));
  await expect(getIntegrationDiagnostic("slack")).rejects.toThrow();
  expect(timeout).toHaveBeenCalledWith(45_000);
  timeout.mockRestore();
});
