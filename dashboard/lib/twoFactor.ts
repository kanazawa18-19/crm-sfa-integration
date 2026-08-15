import * as OTPAuth from "otpauth";
import QRCode from "qrcode";
import { randomBytes } from "crypto";
import { hashPassword, verifyPassword } from "@/lib/adminSession";

// web-engagement-toolのlib/twoFactor.tsと同じ実装(2026-08-15移植)。

const ISSUER = "crm-sfa-integration-dashboard";

export function generateTotpSecret(): string {
  return new OTPAuth.Secret({ size: 20 }).base32;
}

export async function generateTotpQrCodeDataUrl(email: string, base32Secret: string): Promise<string> {
  const totp = new OTPAuth.TOTP({
    issuer: ISSUER,
    label: email,
    secret: OTPAuth.Secret.fromBase32(base32Secret),
  });
  return QRCode.toDataURL(totp.toString());
}

/** Accepts a code from the current or adjacent 30s window (clock drift tolerance). */
export function verifyTotpCode(base32Secret: string, code: string): boolean {
  const totp = new OTPAuth.TOTP({
    issuer: ISSUER,
    secret: OTPAuth.Secret.fromBase32(base32Secret),
  });
  const delta = totp.validate({ token: code.trim(), window: 1 });
  return delta !== null;
}

const BACKUP_CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"; // no 0/O/1/I

function randomBackupCode(): string {
  const bytes = randomBytes(8);
  let code = "";
  for (const b of bytes) {
    code += BACKUP_CODE_CHARS[b % BACKUP_CODE_CHARS.length];
  }
  return `${code.slice(0, 4)}-${code.slice(4, 8)}`;
}

/** Returns 10 plaintext codes to show the user once, plus their hashes to store. */
export function generateBackupCodes(): { plaintext: string[]; hashes: string[] } {
  const plaintext = Array.from({ length: 10 }, randomBackupCode);
  const hashes = plaintext.map((c) => hashPassword(c));
  return { plaintext, hashes };
}

/** Returns the remaining hash list with the matched code removed, or null if no match. */
export function consumeBackupCode(hashes: string[], code: string): string[] | null {
  const normalized = code.trim().toUpperCase();
  const index = hashes.findIndex((h) => verifyPassword(normalized, h));
  if (index === -1) return null;
  return [...hashes.slice(0, index), ...hashes.slice(index + 1)];
}

export const EMAIL_OTP_TTL_MS = 1000 * 60 * 10; // 10分
export const EMAIL_OTP_RESEND_COOLDOWN_MS = 1000 * 30; // 30秒

/** Cryptographically-random 6-digit code, zero-padded (e.g. "003942"). Uses
 * rejection sampling over a uint32 to avoid modulo bias. */
export function generateEmailOtpPlaintext(): string {
  const RANGE = 1_000_000; // 000000-999999
  const MAX_UNBIASED = Math.floor(0x100000000 / RANGE) * RANGE;
  let n: number;
  do {
    n = randomBytes(4).readUInt32BE(0);
  } while (n >= MAX_UNBIASED);
  return String(n % RANGE).padStart(6, "0");
}
