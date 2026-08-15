// IPv4-only allowlist matcher (exact address or CIDR block, e.g.
// "203.0.113.5" or "203.0.113.0/24"). Deliberately dependency-free — this
// only needs to run inside proxy.ts on every request.
// web-engagement-toolのlib/ipAllowlist.tsと同じ実装(2026-08-15移植、
// ALWAYS_ALLOWED_IPSも同じ社内拠点のIPをそのまま流用)。

function ipv4ToInt(ip: string): number | null {
  const parts = ip.trim().split(".");
  if (parts.length !== 4) return null;
  let result = 0;
  for (const part of parts) {
    const n = Number(part);
    if (!Number.isInteger(n) || n < 0 || n > 255) return null;
    result = (result << 8) | n;
  }
  return result >>> 0;
}

function matchesEntry(ip: string, entry: string): boolean {
  const trimmed = entry.trim();
  if (!trimmed) return false;

  if (trimmed.includes("/")) {
    const [base, prefixStr] = trimmed.split("/");
    const prefix = Number(prefixStr);
    const baseInt = ipv4ToInt(base);
    const ipInt = ipv4ToInt(ip);
    if (baseInt == null || ipInt == null || !Number.isInteger(prefix) || prefix < 0 || prefix > 32) return false;
    if (prefix === 0) return true;
    const mask = (~0 << (32 - prefix)) >>> 0;
    return (baseInt & mask) === (ipInt & mask);
  }

  return ip.trim() === trimmed;
}

// Office IPs that must always retain admin access no matter what's
// configured in AppSettings.ipAllowlist — a safeguard against locking out
// the whole company by a misconfigured/cleared DB list. Baked into code
// (not the DB) on purpose so the settings UI can't remove them; changing
// this list requires a code change + deploy. Only takes effect when IP
// restriction is actually turned on — see proxy.ts.
export const ALWAYS_ALLOWED_IPS: { label: string; ip: string }[] = [
  { label: "東京本社", ip: "117.102.183.56" },
  { label: "東京本社", ip: "159.28.76.37" },
  { label: "北海道営業所(201)", ip: "61.205.238.144" },
  { label: "北海道営業所(201)", ip: "133.114.252.212" },
  { label: "北海道営業所(208)", ip: "217.178.63.14" },
  { label: "大阪支店", ip: "163.44.40.134" },
];

export function isIpAllowed(ip: string | null, allowlist: string[]): boolean {
  if (!ip) return false;
  const fullList = [...allowlist, ...ALWAYS_ALLOWED_IPS.map((e) => e.ip)];
  return fullList.some((entry) => matchesEntry(ip, entry));
}

/** First IP in X-Forwarded-For (client's real address before any proxy hops), or null. */
export function extractClientIp(headers: Headers): string | null {
  const forwardedFor = headers.get("x-forwarded-for");
  if (forwardedFor) {
    const first = forwardedFor.split(",")[0]?.trim();
    if (first) return first;
  }
  return headers.get("x-real-ip");
}
