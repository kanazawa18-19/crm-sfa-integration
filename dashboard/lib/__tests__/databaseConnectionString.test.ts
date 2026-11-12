import { describe, expect, it } from "vitest";
import { parse } from "pg-connection-string";
import { databaseConnectionString } from "@/lib/databaseConnectionString";

const base = "postgresql://test:p%40ss@db.example.com:5432/crm";

describe("DB接続の証明書検証", () => {
  it.each(["prefer", "require", "verify-ca"])("%sを現行と同じ完全検証に明示する", (mode) => {
    const result = databaseConnectionString(`${base}?sslmode=${mode}&application_name=crm`)!;
    const config = parse(result);
    expect(config.sslmode).toBe("verify-full");
    expect(config.ssl).toEqual({});
    expect(config.host).toBe("db.example.com");
    expect(config.port).toBe("5432");
    expect(config.user).toBe("test");
    expect(config.password).toBe("p@ss");
    expect(config.database).toBe("crm");
    expect(config.application_name).toBe("crm");
  });

  it.each(["verify-full", "disable", "no-verify"])("明示した%sは変更しない", (mode) => {
    const value = `${base}?sslmode=${mode}`;
    expect(databaseConnectionString(value)).toBe(value);
  });

  it("libpq互換指定を維持する", () => {
    const value = `${base}?sslmode=require&uselibpqcompat=true`;
    expect(databaseConnectionString(value)).toBe(value);
    expect(parse(databaseConnectionString(value)!).ssl).toEqual({ rejectUnauthorized: false });
  });

  it("重複クエリはpgと同じ最後の値で判定する", () => {
    const value = `${base}?sslmode=disable&sslmode=require&uselibpqcompat=true&uselibpqcompat=false`;
    const config = parse(databaseConnectionString(value)!);
    expect(config.sslmode).toBe("verify-full");
    expect(config.ssl).toEqual({});
    const disabled = `${base}?sslmode=require&sslmode=disable`;
    expect(databaseConnectionString(disabled)).toBe(disabled);
  });

  it.each([
    "postgres://test:p ss@host/db?sslmode=require&user=a%2Bb&password=a%25b",
    "postgres://test:p%ss@host/db?sslmode=require&user=a%2Bb&password=a%25b",
  ])("pg独自のエンコード処理が必要な接続文字列は変更しない", (value) => {
    expect(databaseConnectionString(value)).toBe(value);
  });

  it.each([undefined, "", "crm", base, "socket:/var/run/postgresql?sslmode=require"])(
    "TLS別名のない値はそのまま返す: %s", (value) => {
      expect(databaseConnectionString(value)).toBe(value);
    }
  );
});
