import { createHmac, timingSafeEqual } from "crypto";

export const SESSION_COOKIE_NAME = "dashboard_session";

// セッションの有効期間。cookie の maxAge と揃えること。
export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

const SESSION_VALUE = "authenticated";

/**
 * DASHBOARD_PASSWORD / SESSION_SECRET が未設定の場合は常に認証を失敗させる
 * （fail-closed）。
 */
export function isAuthConfigured(): boolean {
  return Boolean(process.env.DASHBOARD_PASSWORD && process.env.SESSION_SECRET);
}

export function verifyPassword(password: string): boolean {
  if (!isAuthConfigured()) {
    return false;
  }
  // タイミング攻撃対策として定数時間比較を行う（Geminiクロスレビューでの指摘を反映）。
  const expectedBuffer = Buffer.from(process.env.DASHBOARD_PASSWORD as string);
  const passwordBuffer = Buffer.from(password);
  if (passwordBuffer.length !== expectedBuffer.length) {
    return false;
  }
  return timingSafeEqual(passwordBuffer, expectedBuffer);
}

/**
 * セッショントークンを発行する。`issuedAt`（発行時刻・ミリ秒）を署名対象に含めることで、
 * 有効期限切れの判定を isValidSessionToken 側で行えるようにする。
 */
export function createSessionToken(issuedAt: number = Date.now()): string | null {
  const secret = process.env.SESSION_SECRET;
  if (!secret) {
    return null;
  }
  const hmac = createHmac("sha256", secret)
    .update(`${SESSION_VALUE}.${issuedAt}`)
    .digest("hex");
  return `${issuedAt}.${hmac}`;
}

export function isValidSessionToken(token: string | undefined | null): boolean {
  if (!token) {
    return false;
  }

  const separatorIndex = token.indexOf(".");
  if (separatorIndex === -1) {
    return false;
  }
  const issuedAt = Number(token.slice(0, separatorIndex));
  if (!Number.isInteger(issuedAt)) {
    return false;
  }

  const expected = createSessionToken(issuedAt);
  if (!expected) {
    return false;
  }
  const tokenBuffer = Buffer.from(token);
  const expectedBuffer = Buffer.from(expected);
  if (tokenBuffer.length !== expectedBuffer.length) {
    return false;
  }
  if (!timingSafeEqual(tokenBuffer, expectedBuffer)) {
    return false;
  }

  const ageMs = Date.now() - issuedAt;
  if (ageMs < 0 || ageMs > SESSION_MAX_AGE_SECONDS * 1000) {
    return false;
  }

  return true;
}
