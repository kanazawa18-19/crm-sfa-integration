import { describe, expect, it } from "vitest";
import { ERROR_MESSAGES_JA, googleOauthErrorMessage } from "@/lib/googleOauthErrors";

describe("googleOauthErrorMessage", () => {
  it("既知のエラーコードには対応する日本語メッセージを返す", () => {
    expect(googleOauthErrorMessage("invalid_state")).toBe(ERROR_MESSAGES_JA.invalid_state);
    expect(googleOauthErrorMessage("exchange_failed")).toBe(ERROR_MESSAGES_JA.exchange_failed);
    expect(googleOauthErrorMessage("scope_denied")).toBe(ERROR_MESSAGES_JA.scope_denied);
  });

  it("未知のエラーコードには汎用メッセージを返す", () => {
    expect(googleOauthErrorMessage("something_unexpected")).toBe(
      "連携に失敗しました。もう一度お試しください。"
    );
  });
});
