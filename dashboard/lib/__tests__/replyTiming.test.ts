import { describe, expect, it } from "vitest";
import {
  NO_EMAIL_LOG_TITLE,
  replyLagCellText,
  replyWindowCellText,
  sampleCountLabel,
} from "@/lib/replyTiming";
import type { ReplyTiming } from "@/lib/backend";

function timing(overrides: Partial<ReplyTiming> = {}): ReplyTiming {
  return {
    sample_size: 3,
    confidence: "low",
    confidence_label: "参考値",
    median_lag_seconds: 3420,
    median_lag_label: "57分",
    mean_lag_seconds: 6360,
    mean_lag_label: "1時間46分",
    fastest_lag_label: "18分",
    slowest_lag_label: "1日4時間",
    inbound_count: 5,
    outbound_count: 20,
    last_inbound_at: null,
    last_outbound_at: null,
    timing: {
      sample_size: 5,
      confidence: "medium",
      confidence_label: "やや不足",
      is_flat: false,
      top_buckets: [
        { label: "09-12時", count: 3 },
        { label: "15-18時", count: 2 },
      ],
      top_weekdays: ["火", "水"],
      buckets: [],
      weekday_counts: [],
    },
    note: "返信3件・受信5件から算出。件数が少ないため参考値です。",
    ...overrides,
  };
}

describe("sampleCountLabel", () => {
  it("件数が十分なときは件数だけを出す", () => {
    expect(sampleCountLabel(12, "high", "十分")).toBe("12件");
  });

  it("やや不足も参考値と同じように注記する(highと見分けが付かないと断定的に読まれる)", () => {
    expect(sampleCountLabel(7, "medium", "やや不足")).toBe("7件・やや不足");
    expect(sampleCountLabel(3, "low", "参考値")).toBe("3件・参考値");
  });
});

describe("replyLagCellText", () => {
  it("ログが無ければnull", () => {
    expect(replyLagCellText(undefined)).toBeNull();
  });

  it("返信ペアが0件ならnull(送信しかしていない連絡先)", () => {
    expect(replyLagCellText(timing({ sample_size: 0 }))).toBeNull();
  });

  it("中央値を主役にし、平均・最速・最遅はホバーへ逃がす", () => {
    const text = replyLagCellText(timing());

    expect(text?.value).toBe("57分");
    expect(text?.sample).toBe("3件・参考値");
    expect(text?.title).toContain("平均 1時間46分");
    expect(text?.title).toContain("最速 18分");
    expect(text?.title).toContain("最遅 1日4時間");
  });
});

describe("replyWindowCellText", () => {
  it("受信が0件ならnull", () => {
    const t = timing();
    expect(replyWindowCellText({ ...t, timing: { ...t.timing, sample_size: 0 } })).toBeNull();
  });

  it("上位の時間帯と曜日を出す", () => {
    const text = replyWindowCellText(timing());

    expect(text?.value).toBe("09-12時 / 15-18時");
    expect(text?.weekdays).toBe("火・水");
    expect(text?.sample).toBe("5件・やや不足");
  });

  it("件数の意味が返信ラグ側と別物であることをホバーで説明する", () => {
    const text = replyWindowCellText(timing());

    expect(text?.title).toContain("受信の総数");
    expect(text?.title).toContain("返信ペア数");
  });

  it("受信が散らばっているときは「傾向なし」と書く（無い傾向をあると読ませない）", () => {
    const t = timing();
    const text = replyWindowCellText({
      ...t,
      timing: { ...t.timing, is_flat: true, top_buckets: [] },
    });

    expect(text?.value).toBe("傾向なし");
    expect(text?.sample).toBe("5件・やや不足");
  });

  it("上位の時間帯が空でも落ちない", () => {
    const t = timing();
    const text = replyWindowCellText({
      ...t,
      timing: { ...t.timing, top_buckets: [], top_weekdays: [] },
    });

    expect(text?.value).toBe("-");
    expect(text?.weekdays).toBe("");
  });
});

describe("NO_EMAIL_LOG_TITLE", () => {
  it("ログが無いときの説明文が用意されている", () => {
    expect(NO_EMAIL_LOG_TITLE).toContain("送受信ログ");
  });
});
