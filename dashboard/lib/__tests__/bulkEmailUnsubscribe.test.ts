import { describe, expect, it } from "vitest";
import {
  buildUnsubscribeToken,
  normalizeContactPageId,
  toDashedContactPageId,
  verifyUnsubscribeToken,
} from "@/lib/bulkEmailUnsubscribe";

const SECRET = "test-secret";
const PAGE_ID = "3ced8ea8-1234-814a-83ce-cb3645539acd";

describe("normalizeContactPageId", () => {
  it("ハイフンと大文字を落とす", () => {
    expect(normalizeContactPageId(" 3CED8EA8-1234 ")).toBe("3ced8ea81234");
  });
});

describe("buildUnsubscribeToken", () => {
  // **Python側(src/bulk_email/unsubscribe.py)が実際に発行した値**を固定値として置いている。
  // リンクを発行するのはPython、検証するのはTypeScriptなので、この2つが一致しなくなると
  // 「本文には配信停止リンクが載っているのに、開いても止められない」という、送った後にしか
  // 気づけない壊れ方をする。どちらかを直したらこの値も必ず取り直すこと。
  it("Python側が発行した署名と一致する", () => {
    expect(buildUnsubscribeToken(SECRET, PAGE_ID)).toBe(
      "Uhektz3Z2HK2TAPzbDZbnQvwX-Q3uen65--WCUTHyIc"
    );
  });

  it("ハイフンの有無で署名が変わらない", () => {
    expect(buildUnsubscribeToken(SECRET, PAGE_ID)).toBe(
      buildUnsubscribeToken(SECRET, PAGE_ID.replaceAll("-", "").toUpperCase())
    );
  });

  it("鍵が無ければ例外", () => {
    expect(() => buildUnsubscribeToken("", PAGE_ID)).toThrow();
  });
});

describe("verifyUnsubscribeToken", () => {
  it("正しい署名を通す", () => {
    expect(verifyUnsubscribeToken(SECRET, PAGE_ID, buildUnsubscribeToken(SECRET, PAGE_ID))).toBe(true);
  });

  it("鍵が違えば通さない", () => {
    expect(verifyUnsubscribeToken("別の鍵", PAGE_ID, buildUnsubscribeToken(SECRET, PAGE_ID))).toBe(
      false
    );
  });

  it("他人のページIDでは通さない", () => {
    expect(
      verifyUnsubscribeToken(SECRET, "00000000-0000-0000-0000-000000000000", buildUnsubscribeToken(SECRET, PAGE_ID))
    ).toBe(false);
  });

  it("長さが違う署名でも例外にせず false を返す", () => {
    // timingSafeEqualは長さが違うと例外を投げる。ここで落ちると、
    // 壊れたURLを開いただけで500になる。
    expect(verifyUnsubscribeToken(SECRET, PAGE_ID, "短い")).toBe(false);
  });

  it("鍵や署名が空なら通さない", () => {
    expect(verifyUnsubscribeToken("", PAGE_ID, "なんでも")).toBe(false);
    expect(verifyUnsubscribeToken(SECRET, PAGE_ID, null)).toBe(false);
  });
});

describe("toDashedContactPageId", () => {
  it("UUIDの区切りを戻す", () => {
    expect(toDashedContactPageId("3ced8ea81234814a83cecb3645539acd")).toBe(PAGE_ID);
  });

  it("32桁の16進でなければ null", () => {
    expect(toDashedContactPageId("みじかい")).toBeNull();
    expect(toDashedContactPageId(PAGE_ID)).toBeNull();
  });
});
