import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSessionToken,
  verifySessionToken,
  isValidSessionToken,
  createPending2FAToken,
  verifyPending2FAToken,
  hashPassword,
  verifyPassword,
} from "@/lib/adminSession";

const ORIGINAL_ENV = { ...process.env };
const USER_ID = "user_123";

describe("adminSession — session token", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.SESSION_SECRET = "test-secret";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.useRealTimers();
  });

  it("createSessionToken で発行したトークンは verifySessionToken/isValidSessionToken で有効になり、userIdが復元できる（往復確認）", () => {
    const token = createSessionToken(USER_ID);
    expect(isValidSessionToken(token)).toBe(true);
    expect(verifySessionToken(token)).toEqual({ userId: USER_ID });
  });

  it("SESSION_SECRET を変えると検証が false になる", () => {
    const token = createSessionToken(USER_ID);
    process.env.SESSION_SECRET = "different-secret";
    expect(isValidSessionToken(token)).toBe(false);
  });

  it("SESSION_SECRET 未設定時は createSessionToken が例外を投げる", () => {
    delete process.env.SESSION_SECRET;
    expect(() => createSessionToken(USER_ID)).toThrow("SESSION_SECRET is not set");
  });

  it("null / undefined / 空文字は false になる", () => {
    expect(isValidSessionToken(null)).toBe(false);
    expect(isValidSessionToken(undefined)).toBe(false);
    expect(isValidSessionToken("")).toBe(false);
  });

  it("区切り文字（.）の数が不正な改ざんトークンは false になる", () => {
    expect(isValidSessionToken("not-a-valid-token")).toBe(false);
    expect(isValidSessionToken("a.b")).toBe(false);
  });

  it("別のHMAC値に差し替えられた改ざんトークンは false になる", () => {
    const token = createSessionToken(USER_ID);
    const [userId, expiresAt] = token.split(".");
    const tamperedHmac = "0".repeat(64);
    expect(isValidSessionToken(`${userId}.${expiresAt}.${tamperedHmac}`)).toBe(false);
  });

  it("userId部分だけ差し替えた改ざんトークンは false になる（署名は別ユーザーIDのものなので不一致）", () => {
    const token = createSessionToken(USER_ID);
    const [, expiresAt, sig] = token.split(".");
    expect(isValidSessionToken(`other_user.${expiresAt}.${sig}`)).toBe(false);
  });

  it("有効期限内のトークンは true になる", () => {
    const now = 1_700_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const token = createSessionToken(USER_ID);

    vi.setSystemTime(now + 1000 * 60 * 60 * 24 * 7 - 1000);
    expect(isValidSessionToken(token)).toBe(true);
  });

  it("有効期限を過ぎたトークンは false になる", () => {
    const now = 1_700_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const token = createSessionToken(USER_ID);

    vi.setSystemTime(now + 1000 * 60 * 60 * 24 * 7 + 1000);
    expect(isValidSessionToken(token)).toBe(false);
  });
});

describe("adminSession — pending 2FA token", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.SESSION_SECRET = "test-secret";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.useRealTimers();
  });

  it("createPending2FAToken で発行したトークンはverifyPending2FATokenで有効になる（往復確認）", () => {
    const token = createPending2FAToken(USER_ID);
    expect(verifyPending2FAToken(token)).toEqual({ userId: USER_ID });
  });

  it("通常のセッショントークンはverifyPending2FATokenでは無効（スコープが別）", () => {
    const sessionToken = createSessionToken(USER_ID);
    expect(verifyPending2FAToken(sessionToken)).toBeNull();
  });

  it("pending 2FAトークンは通常のverifySessionTokenでは無効（スコープが別）", () => {
    const pendingToken = createPending2FAToken(USER_ID);
    expect(verifySessionToken(pendingToken)).toBeNull();
  });

  it("10分の有効期限を過ぎるとfalseになる", () => {
    const now = 1_700_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const token = createPending2FAToken(USER_ID);

    vi.setSystemTime(now + 1000 * 60 * 10 + 1000);
    expect(verifyPending2FAToken(token)).toBeNull();
  });
});

describe("adminSession — password hashing", () => {
  it("正しいパスワードはverifyPasswordでtrueになる", () => {
    const hash = hashPassword("correct-horse-battery-staple");
    expect(verifyPassword("correct-horse-battery-staple", hash)).toBe(true);
  });

  it("間違ったパスワードはfalseになる", () => {
    const hash = hashPassword("correct-horse-battery-staple");
    expect(verifyPassword("wrong-password", hash)).toBe(false);
  });

  it("保存値がnull/undefinedの場合はfalseになる", () => {
    expect(verifyPassword("anything", null)).toBe(false);
    expect(verifyPassword("anything", undefined)).toBe(false);
  });

  it("同じパスワードでも毎回異なるsaltでハッシュ化される", () => {
    const hash1 = hashPassword("same-password");
    const hash2 = hashPassword("same-password");
    expect(hash1).not.toBe(hash2);
    expect(verifyPassword("same-password", hash1)).toBe(true);
    expect(verifyPassword("same-password", hash2)).toBe(true);
  });
});
