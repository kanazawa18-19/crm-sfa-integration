import { requireRole } from "@/lib/auth";
import ConsentPageClient from "./ConsentPageClient";

// 送信根拠(オプトイン)の管理(2026-09-03)。
//
// 一斉配信は「送ってよい根拠が登録されている連絡先」にしか送らない(既定で送信不可)。
// その根拠をここで1件ずつ登録する。判定の定義は src/bulk_email/consent.py が正本。
//
// 閲覧者(viewer)は入れない。連絡先が氏名・アドレスごと並ぶ上に、
// 「送ってよい」という判断そのものを記録する画面のため。
export const dynamic = "force-dynamic";

export default async function BulkEmailConsentPage() {
  await requireRole("editor");
  return <ConsentPageClient />;
}
