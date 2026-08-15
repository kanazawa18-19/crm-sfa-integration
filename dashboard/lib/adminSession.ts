import { createHmac, timingSafeEqual, scryptSync, randomBytes } from "crypto";

// web-engagement-toolのlib/adminSession.tsと同じ実装(2026-08-15移植、ユーザー管理・2FA
// 導入に伴い、パスワード1本の旧セッション方式(旧lib/auth.ts)を置き換える)。
// 署名鍵は新規に増やさず、既存のSESSION_SECRET環境変数をそのまま流用する。

export const COOKIE_NAME = "admin_session";
const SESSION_TTL_MS = 1000 * 60 * 60 * 24 * 7; // 7 days

export const PENDING_2FA_COOKIE_NAME = "admin_2fa_pending";
const PENDING_2FA_TTL_MS = 1000 * 60 * 10; // 10 minutes — just long enough to enter a code

function sign(payload: string): string {
  const secret = process.env.SESSION_SECRET;
  if (!secret) throw new Error("SESSION_SECRET is not set");
  return createHmac("sha256", secret).update(payload).digest("hex");
}

/** Session token payload is `${userId}.${expiresAt}` — proxy.ts can verify
 * the signature and expiry without a DB call; Server Components / Actions
 * decode the userId and look the User up fresh for an authoritative role
 * check (so a role change or deletion takes effect immediately). */
export function createSessionToken(userId: string): string {
  const expiresAt = Date.now() + SESSION_TTL_MS;
  const payload = `${userId}.${expiresAt}`;
  return `${payload}.${sign(payload)}`;
}

export function verifySessionToken(token: string | undefined | null): { userId: string } | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [userId, expiresAt, sig] = parts;
  if (!userId || !expiresAt || !sig) return null;

  const payload = `${userId}.${expiresAt}`;
  const expected = sign(payload);
  const sigBuf = Buffer.from(sig);
  const expectedBuf = Buffer.from(expected);
  if (sigBuf.length !== expectedBuf.length) return null;
  if (!timingSafeEqual(sigBuf, expectedBuf)) return null;

  if (Number(expiresAt) <= Date.now()) return null;
  return { userId };
}

export function isValidSessionToken(token: string | undefined | null): boolean {
  return verifySessionToken(token) !== null;
}

/** Short-lived token proving "this browser just passed the password check for
 * this user," issued between password login and TOTP/email-OTP verification
 * — deliberately a separate cookie/signature scope from the full session so
 * proxy.ts never mistakes a pending-2FA visitor for an authenticated one. */
export function createPending2FAToken(userId: string): string {
  const expiresAt = Date.now() + PENDING_2FA_TTL_MS;
  const payload = `2fa.${userId}.${expiresAt}`;
  return `${payload}.${sign(payload)}`;
}

export function verifyPending2FAToken(token: string | undefined | null): { userId: string } | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 4 || parts[0] !== "2fa") return null;
  const [, userId, expiresAt, sig] = parts;
  if (!userId || !expiresAt || !sig) return null;

  const payload = `2fa.${userId}.${expiresAt}`;
  const expected = sign(payload);
  const sigBuf = Buffer.from(sig);
  const expectedBuf = Buffer.from(expected);
  if (sigBuf.length !== expectedBuf.length) return null;
  if (!timingSafeEqual(sigBuf, expectedBuf)) return null;

  if (Number(expiresAt) <= Date.now()) return null;
  return { userId };
}

export function hashPassword(password: string): string {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(password, salt, 64).toString("hex");
  return `${salt}:${hash}`;
}

export function verifyPassword(password: string, stored: string | null | undefined): boolean {
  if (!stored) return false;
  const [salt, hash] = stored.split(":");
  if (!salt || !hash) return false;
  const hashBuffer = Buffer.from(hash, "hex");
  const candidateBuffer = scryptSync(password, salt, 64);
  if (hashBuffer.length !== candidateBuffer.length) return false;
  return timingSafeEqual(hashBuffer, candidateBuffer);
}
