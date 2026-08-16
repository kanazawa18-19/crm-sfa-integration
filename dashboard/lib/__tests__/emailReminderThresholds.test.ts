import { describe, expect, it } from "vitest";
import { EMAIL_REMINDER_THRESHOLD_OPTIONS } from "@/lib/emailReminderThresholds";

describe("EMAIL_REMINDER_THRESHOLD_OPTIONS", () => {
  it("3時間刻みで3〜72時間の24個を返す", () => {
    expect(EMAIL_REMINDER_THRESHOLD_OPTIONS).toHaveLength(24);
    expect(EMAIL_REMINDER_THRESHOLD_OPTIONS[0]).toBe(3);
    expect(EMAIL_REMINDER_THRESHOLD_OPTIONS.at(-1)).toBe(72);
  });

  it("すべて3の倍数である", () => {
    expect(EMAIL_REMINDER_THRESHOLD_OPTIONS.every((hours) => hours % 3 === 0)).toBe(true);
  });
});
