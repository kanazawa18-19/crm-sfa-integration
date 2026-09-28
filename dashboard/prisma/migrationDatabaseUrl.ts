// 接続先や認証情報をエラーに出さず、マイグレーション専用の直接接続を選ぶ。
export function migrationDatabaseUrl(env: Record<string, string | undefined>): string | undefined {
  const direct = env.DATABASE_URL_UNPOOLED?.trim();
  const fallback = env.DATABASE_URL?.trim();
  const value = direct || fallback;
  // 接続不要のprisma generateは許可する。migrateはURL未設定としてPrisma側で停止する。
  if (!value) return undefined;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("マイグレーション用DB接続の形式を確認してください");
  }
  if (!["postgres:", "postgresql:"].includes(parsed.protocol) || !parsed.hostname
      || parsed.hostname.includes(",") || parsed.hostname.toLowerCase().includes("pooler")
      || parsed.searchParams.has("host") || parsed.searchParams.has("hostaddr")
      || parsed.searchParams.has("pgbouncer")) {
    throw new Error("マイグレーションには接続先を上書きしない直接接続が必要です");
  }
  if (!direct && !["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname)) {
    throw new Error("マイグレーションにはDATABASE_URL_UNPOOLEDを設定してください");
  }
  return value;
}
