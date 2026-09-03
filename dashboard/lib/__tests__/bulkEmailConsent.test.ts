import { describe, expect, it } from "vitest";
import {
  businessToday,
  CONSENT_BASES,
  isConsentBasis,
  isNormalizedContactPageId,
  normalizeContactPageIdForConsent,
  parseObtainedAt,
} from "@/lib/bulkEmailConsent";

// 「送ってよい根拠」の入力検証(2026-09-03)。
//
// ここが緩むと、画面から任意の値を保存できてしまう。種類の一覧は
// src/bulk_email/consent.py の BASIS_LABELS と揃っている必要があり、
// ズレるとバックエンドが「種類が不明」として送信不可にする。

const NOW = new Date("2026-09-03T00:00:00.000Z");

describe("根拠の種類", () => {
  it("Python側(consent.py)と同じ4種類を持つ", () => {
    expect([...CONSENT_BASES]).toEqual(["opt_in", "notified", "transaction", "published"]);
  });

  it("知らない値は受け付けない", () => {
    expect(isConsentBasis("notified")).toBe(true);
    expect(isConsentBasis("むかしの種類")).toBe(false);
    expect(isConsentBasis(undefined)).toBe(false);
  });
});

describe("連絡先ページIDの正規化", () => {
  it("ハイフン無し・小文字に揃える", () => {
    expect(normalizeContactPageIdForConsent(" 3CED8EA8-1234-814A-83CE-CB3645539ACD ")).toBe(
      "3ced8ea81234814a83cecb3645539acd"
    );
  });

  it("32桁hexでなければ弾く", () => {
    expect(isNormalizedContactPageId("3ced8ea81234814a83cecb3645539acd")).toBe(true);
    expect(isNormalizedContactPageId("3ced8ea8-1234-814a-83ce-cb3645539acd")).toBe(false);
    expect(isNormalizedContactPageId("")).toBe(false);
  });
});

describe("取得日", () => {
  it("YYYY-MM-DD をそのまま返す（時刻に変換しない）", () => {
    expect(parseObtainedAt("2026-04-08", NOW)).toBe("2026-04-08");
  });

  it("未来の日付は受け付けない", () => {
    // 「明日 名刺交換した」根拠は成立しない。年の打ち間違いもここで止まる。
    expect(parseObtainedAt("2062-04-08", NOW)).toBeNull();
  });

  it("★明日の日付を受け付けない（日本時間の午前中でも）", () => {
    // 以前は「今+24時間」と比べていたため、JSTの午前中は翌日が通っていた。
    // しかもその日はPython側が未来日として弾くので、翌日になった瞬間に
    // 人の再確認なしで有効化される、という壊れ方だった。
    const 朝 = new Date("2026-09-02T23:30:00.000Z"); // JST 9/3 08:30
    expect(parseObtainedAt("2026-09-04", 朝)).toBeNull();
    expect(parseObtainedAt("2026-09-03", 朝)).toBe("2026-09-03");
  });

  it("業務上の今日はJSTで決まる", () => {
    // JST 9/3 08:30 の時点で、UTCではまだ9/2。UTC基準だと当日が未来日になる。
    expect(businessToday(new Date("2026-09-02T23:30:00.000Z"))).toBe("2026-09-03");
  });

  it("実在しない日付は受け付けない", () => {
    // Date は 2026-02-31 を 3月に繰り上げるので、Number.isNaN だけでは弾けない。
    expect(parseObtainedAt("2026-02-31", NOW)).toBeNull();
    expect(parseObtainedAt("2026-13-01", NOW)).toBeNull();
  });

  it("形式が違えば受け付けない", () => {
    expect(parseObtainedAt("2026/04/08", NOW)).toBeNull();
    expect(parseObtainedAt("", NOW)).toBeNull();
  });

  it("3年より古くても登録はできる（古さは警告であって無効ではない）", () => {
    expect(parseObtainedAt("2015-01-01", NOW)).not.toBeNull();
  });
});
