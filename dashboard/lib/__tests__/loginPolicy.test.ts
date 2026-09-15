import { describe, expect, it } from "vitest";
import {
  ALLOWED_LOGIN_DOMAIN,
  DOMAIN_REJECTED_MESSAGE,
  HOSTED_DOMAIN_REJECTED_MESSAGE,
  checkGoogleAccountDomain,
  emailDomainOf,
  isActivatedUser,
  isAllowedLoginEmail,
} from "@/lib/loginPolicy";

describe("ログインを許すドメインの判定（cnctor.jp の Google アカウントだけ）", () => {
  it("許すドメインは cnctor.jp に固定されている", () => {
    expect(ALLOWED_LOGIN_DOMAIN).toBe("cnctor.jp");
  });

  it("メールのドメインを小文字で取り出す。形が崩れていれば null", () => {
    expect(emailDomainOf("Taro@CNCTOR.jp")).toBe("cnctor.jp");
    expect(emailDomainOf("  taro@cnctor.jp ")).toBe("cnctor.jp");
    expect(emailDomainOf("taro")).toBeNull();
    expect(emailDomainOf("@cnctor.jp")).toBeNull();
    expect(emailDomainOf("taro@")).toBeNull();
  });

  it("招待できるのは cnctor.jp のアドレスだけ", () => {
    expect(isAllowedLoginEmail("taro@cnctor.jp")).toBe(true);
    expect(isAllowedLoginEmail("taro@gmail.com")).toBe(false);
    expect(isAllowedLoginEmail("taro@cnctor.jp.example.com")).toBe(false);
    expect(isAllowedLoginEmail("taro@sub.cnctor.jp")).toBe(false);
  });

  it("形が崩れたアドレスは、ドメインが cnctor.jp でも招待できない（@ が 2 つ・空白・ローカル部なし）", () => {
    expect(isAllowedLoginEmail("a@evil@cnctor.jp")).toBe(false);
    expect(isAllowedLoginEmail("taro tanaka@cnctor.jp")).toBe(false);
    expect(isAllowedLoginEmail("@cnctor.jp")).toBe(false);
    expect(isAllowedLoginEmail(" Taro@CNCTOR.jp ")).toBe(true);
  });

  it("メールも Workspace の所属ドメインも cnctor.jp なら通す", () => {
    expect(checkGoogleAccountDomain({ email: "taro@cnctor.jp", hostedDomain: "cnctor.jp" })).toEqual({ ok: true });
    expect(checkGoogleAccountDomain({ email: "Taro@Cnctor.JP", hostedDomain: "CNCTOR.JP" })).toEqual({ ok: true });
  });

  it("他ドメインのメールは、hd が何であれ拒否する", () => {
    expect(checkGoogleAccountDomain({ email: "taro@gmail.com", hostedDomain: "cnctor.jp" })).toEqual({
      ok: false,
      reason: DOMAIN_REJECTED_MESSAGE,
    });
    expect(checkGoogleAccountDomain({ email: "taro@example.com", hostedDomain: null })).toEqual({
      ok: false,
      reason: DOMAIN_REJECTED_MESSAGE,
    });
  });

  it("メールが cnctor.jp でも、Workspace の所属ドメインが無い・違うなら拒否する（個人アカウント対策）", () => {
    expect(checkGoogleAccountDomain({ email: "taro@cnctor.jp" })).toEqual({
      ok: false,
      reason: HOSTED_DOMAIN_REJECTED_MESSAGE,
    });
    expect(checkGoogleAccountDomain({ email: "taro@cnctor.jp", hostedDomain: null })).toEqual({
      ok: false,
      reason: HOSTED_DOMAIN_REJECTED_MESSAGE,
    });
    expect(checkGoogleAccountDomain({ email: "taro@cnctor.jp", hostedDomain: "other.example" })).toEqual({
      ok: false,
      reason: HOSTED_DOMAIN_REJECTED_MESSAGE,
    });
  });
});

describe("招待を承諾して有効になったユーザーの判定（削除保護・招待中表示が使う）", () => {
  it("一度もログインしておらずパスワードも無ければ「招待中」", () => {
    expect(isActivatedUser({ passwordHash: null, lastLoginAt: null })).toBe(false);
    expect(isActivatedUser({ passwordHash: null, lastLoginAt: null, googleSubject: null })).toBe(false);
  });

  it("Google でログインしたことがあれば、パスワードが無くても有効", () => {
    expect(isActivatedUser({ passwordHash: null, lastLoginAt: new Date("2026-09-16T00:00:00Z") })).toBe(true);
  });

  it("旧いパスワード持ち・Google 束縛済みも有効のまま（既存ユーザーの扱いを変えない）", () => {
    expect(isActivatedUser({ passwordHash: "salt:hash", lastLoginAt: null })).toBe(true);
    expect(isActivatedUser({ passwordHash: null, lastLoginAt: null, googleSubject: "sub-1" })).toBe(true);
  });
});
