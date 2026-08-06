import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SESSION_MAX_AGE_SECONDS,
  createSessionToken,
  isValidSessionToken,
} from "@/lib/auth";

const ORIGINAL_ENV = { ...process.env };

describe("auth", () => {
  beforeEach(() => {
    process.env = { ...ORIGINAL_ENV };
    process.env.SESSION_SECRET = "test-secret";
  });

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
    vi.useRealTimers();
  });

  it("createSessionToken で発行したトークンは isValidSessionToken で true になる（往復確認）", () => {
    const token = createSessionToken();
    expect(token).not.toBeNull();
    expect(isValidSessionToken(token)).toBe(true);
  });

  it("SESSION_SECRET を変えると検証が false になる", () => {
    const token = createSessionToken();
    process.env.SESSION_SECRET = "different-secret";
    expect(isValidSessionToken(token)).toBe(false);
  });

  it("SESSION_SECRET 未設定時は createSessionToken が null を返す", () => {
    delete process.env.SESSION_SECRET;
    expect(createSessionToken()).toBeNull();
  });

  it("SESSION_SECRET 未設定時は isValidSessionToken が例外を投げず常に false になる（fail-closed）", () => {
    process.env.SESSION_SECRET = "test-secret";
    const token = createSessionToken();
    delete process.env.SESSION_SECRET;
    expect(() => isValidSessionToken(token)).not.toThrow();
    expect(isValidSessionToken(token)).toBe(false);
  });

  it("null / undefined / 空文字は false になる", () => {
    expect(isValidSessionToken(null)).toBe(false);
    expect(isValidSessionToken(undefined)).toBe(false);
    expect(isValidSessionToken("")).toBe(false);
  });

  it("区切り文字（.）が無い改ざんされたトークンは false になる", () => {
    expect(isValidSessionToken("not-a-valid-token")).toBe(false);
  });

  it("長さの異なる改ざんされたトークンは false になる", () => {
    const token = createSessionToken();
    expect(token).not.toBeNull();
    expect(isValidSessionToken(`${token}extra`)).toBe(false);
  });

  it("別のHMAC値に差し替えられた改ざんトークンは false になる", () => {
    const token = createSessionToken();
    expect(token).not.toBeNull();
    const [issuedAt] = (token as string).split(".");
    const tamperedHmac = "0".repeat(64);
    expect(isValidSessionToken(`${issuedAt}.${tamperedHmac}`)).toBe(false);
  });

  it("有効期限（SESSION_MAX_AGE_SECONDS）内のトークンは true になる", () => {
    const now = 1_700_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const token = createSessionToken();

    vi.setSystemTime(now + SESSION_MAX_AGE_SECONDS * 1000 - 1000);
    expect(isValidSessionToken(token)).toBe(true);
  });

  it("有効期限を過ぎたトークンは false になる", () => {
    const now = 1_700_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const token = createSessionToken();

    vi.setSystemTime(now + SESSION_MAX_AGE_SECONDS * 1000 + 1000);
    expect(isValidSessionToken(token)).toBe(false);
  });
});
