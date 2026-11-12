// pgの現行動作（証明書・ホスト名の検証）を将来の更新でも維持する。
export function databaseConnectionString(value: string | undefined): string | undefined {
  if (!value) return value;
  // pg独自の事前エンコードが必要な文字列は、そのままpgへ渡す。
  if (/\s|%(?![\da-f]{2})/i.test(value)) return value;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return value;
  }
  if (!["postgres:", "postgresql:"].includes(parsed.protocol)) return value;
  // pgは重複するクエリの最後の値を採用する。明示的なlibpq互換設定は変えない。
  const last = (key: string) => parsed.searchParams.getAll(key).slice(-1)[0];
  if (last("uselibpqcompat") === "true") return value;
  if (!["prefer", "require", "verify-ca"].includes(last("sslmode"))) return value;
  parsed.searchParams.set("sslmode", "verify-full");
  return parsed.toString();
}
