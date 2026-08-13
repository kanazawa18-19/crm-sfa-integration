import { describe, expect, it } from "vitest";
import { isSessionExpiredResponse } from "@/lib/sessionCheck";

describe("isSessionExpiredResponse", () => {
  it("response.typeがopaqueredirectの場合trueを返す", () => {
    const response = { type: "opaqueredirect", status: 200 } as unknown as Response;
    expect(isSessionExpiredResponse(response)).toBe(true);
  });

  it("response.statusが0の場合trueを返す", () => {
    const response = { type: "basic", status: 0 } as unknown as Response;
    expect(isSessionExpiredResponse(response)).toBe(true);
  });

  it("通常のレスポンスの場合falseを返す", () => {
    const response = { type: "basic", status: 200 } as unknown as Response;
    expect(isSessionExpiredResponse(response)).toBe(false);
  });

  it("通常のエラーレスポンス（401等）の場合falseを返す", () => {
    const response = { type: "basic", status: 401 } as unknown as Response;
    expect(isSessionExpiredResponse(response)).toBe(false);
  });
});
