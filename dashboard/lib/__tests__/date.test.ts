import { describe, expect, it } from "vitest";
import { formatJst } from "@/lib/date";

describe("formatJst", () => {
  it("サーバーのタイムゾーンに関わらずJSTで整形する", () => {
    // UTC 2026-08-27T15:30:00Z は JST 2026-08-28 00:30
    const date = new Date("2026-08-27T15:30:00Z");
    const formatted = formatJst(date);

    expect(formatted).toContain("2026");
    expect(formatted).toContain("28");
    expect(formatted).toContain("0:30");
  });
});
