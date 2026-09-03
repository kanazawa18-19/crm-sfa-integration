import { createHmac, timingSafeEqual } from "node:crypto";

// 一斉配信の配信停止リンクの検証(2026-09-03)。
//
// リンクを**発行するのはPython側**(src/bulk_email/unsubscribe.py)で、**検証するのは
// ここ**(お客様が開く公開ページ /unsubscribe)。同じ鍵・同じ正規化・同じHMACである
// ことが前提で、片方だけ直すと発行済みリンクが全部無効になる。仕様を変えるときは
// 必ず両方を直すこと。
//
//    Python  HMAC-SHA256(BULK_EMAIL_UNSUBSCRIBE_SECRET, 正規化したページID)
//            → URLセーフbase64(パディング無し) → 本文のURLに載る
//    ここ    同じ計算をやり直して一致を見るだけ。DBは引かない
//
// トークンをDBに貯めない理由(連絡先3,782件ぶんの行を先に作らない)はPython側の
// モジュールdocstringにある。

const SECRET_ENV_VAR = "BULK_EMAIL_UNSUBSCRIBE_SECRET";

/** NotionページIDの表記ゆれ(ハイフン有無・大文字小文字)を吸収する。src/bulk_email/ids.pyと同じ。 */
export function normalizeContactPageId(pageId: string): string {
  return (pageId ?? "").trim().toLowerCase().replaceAll("-", "");
}

export function loadUnsubscribeSecret(): string {
  return (process.env[SECRET_ENV_VAR] ?? "").trim();
}

export function buildUnsubscribeToken(secret: string, contactPageId: string): string {
  if (!secret) throw new Error(`${SECRET_ENV_VAR} is not set`);
  return createHmac("sha256", secret).update(normalizeContactPageId(contactPageId)).digest("base64url");
}

/**
 * 署名が正しいか。
 *
 * 比較はtimingSafeEqualで行う(先頭何文字まで合っているかを応答時間から漏らさない)。
 * 長さが違う場合はtimingSafeEqualが例外を投げるため、先に長さを見て落とす
 * — 長さの一致・不一致だけは漏れるが、HMACの出力長は固定なので情報量が無い。
 */
export function verifyUnsubscribeToken(
  secret: string,
  contactPageId: string,
  token: string | null | undefined
): boolean {
  if (!secret || !token) return false;
  let expected: string;
  try {
    expected = buildUnsubscribeToken(secret, contactPageId);
  } catch {
    return false;
  }
  const expectedBuffer = Buffer.from(expected, "utf8");
  const actualBuffer = Buffer.from(token, "utf8");
  if (expectedBuffer.length !== actualBuffer.length) return false;
  return timingSafeEqual(expectedBuffer, actualBuffer);
}

/**
 * 正規化済み(ハイフン無し32桁)のページIDを、NotionのUUID表記(8-4-4-4-12)へ戻す。
 *
 * 配信停止URLに載るのは正規化した形だが、`EmailLog.contactPageId`はNotionから来た
 * ハイフン付きの形で入っている。ハイフンを外して比較しようとすると`replace()`が要り、
 * 行が増え続けるテーブルの全走査になる。UUIDの区切り位置は決まっているので、
 * ここで元の形へ戻して**インデックスの効く等値比較**にする。
 *
 * 32桁の16進でなければnullを返す(将来IDの形が変わったときに、黙って壊れた文字列で
 * 検索しないため)。
 */
export function toDashedContactPageId(normalizedPageId: string): string | null {
  if (!/^[0-9a-f]{32}$/.test(normalizedPageId)) return null;
  return [
    normalizedPageId.slice(0, 8),
    normalizedPageId.slice(8, 12),
    normalizedPageId.slice(12, 16),
    normalizedPageId.slice(16, 20),
    normalizedPageId.slice(20),
  ].join("-");
}
