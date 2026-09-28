import { describe, expect, it } from "vitest";
import { migrationDatabaseUrl } from "../migrationDatabaseUrl";

const direct = "postgresql://user:secret@ep-example.aws.neon.tech/db?sslmode=require";
const pooled = "postgresql://user:secret@ep-example-pooler.aws.neon.tech/db?sslmode=require";

describe("マイグレーションは直接接続を使う", () => {
  it("通常接続と両方ある場合はUNPOOLEDを優先する", () => {
    expect(migrationDatabaseUrl({ DATABASE_URL: pooled, DATABASE_URL_UNPOOLED: direct })).toBe(direct);
  });
  it.each([pooled, direct])("remoteの通常接続へ暗黙に戻らない", (url) => {
    expect(() => migrationDatabaseUrl({ DATABASE_URL: url })).toThrow();
  });
  it("UNPOOLEDという名前でもpooler接続は拒否する", () => {
    expect(() => migrationDatabaseUrl({ DATABASE_URL_UNPOOLED: pooled })).toThrow("直接接続");
  });
  it.each(["host=other", "hostaddr=127.0.0.1", "pgbouncer=true"])("接続先の上書きを拒否する: %s", (query) => {
    expect(() => migrationDatabaseUrl({ DATABASE_URL_UNPOOLED: direct + "&" + query })).toThrow("直接接続");
  });
  it("ローカルだけ通常URLを使用できる", () => {
    const local = "postgresql://user@127.0.0.1:55443/test";
    expect(migrationDatabaseUrl({ DATABASE_URL: local })).toBe(local);
  });
  it("接続不要のgenerate用に未設定を保持する", () => {
    expect(migrationDatabaseUrl({})).toBeUndefined();
  });
  it("異常値をエラー文に含めない", () => {
    expect(() => migrationDatabaseUrl({ DATABASE_URL_UNPOOLED: "private-secret" })).toThrow("形式");
    try {
      migrationDatabaseUrl({ DATABASE_URL_UNPOOLED: "private-secret" });
    } catch (error) {
      expect(String(error)).not.toContain("private-secret");
    }
  });
});
