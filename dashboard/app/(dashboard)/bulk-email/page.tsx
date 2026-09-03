import { requireRole } from "@/lib/auth";
import BulkEmailPageClient from "./BulkEmailPageClient";

// 一斉配信(2026-09-03)。**今あるのはプレビューだけで、1通も送らない。**
// 送信経路(Gmail APIにgmail.sendを足すか)が決まるまで送信機能は作らない
// (docs/bulk_email_design_note.md「出す順番（段階リリース）」)。
//
// 閲覧者(viewer)は入れない。営業メールの下書きを作る場であり、
// 取引先の連絡先が氏名・アドレスごと一覧で並ぶため。
export const dynamic = "force-dynamic";

export default async function BulkEmailPage() {
  const user = await requireRole("editor");
  return <BulkEmailPageClient senderName={user.name ?? ""} />;
}
