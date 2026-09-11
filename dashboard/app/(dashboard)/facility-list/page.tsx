import { requireRole } from "@/lib/auth";
import FacilityListPageClient from "./FacilityListPageClient";

// リスト作成マシーン(2026-09-11)。楽天トラベルの公開ページから取り込んだ宿泊施設を
// 条件で絞り、CRMと突合して営業リスト(CSV)にする。
//
// 閲覧者(viewer)は入れない。書き出したリストに取引先の担当者名・メールアドレス・
// 電話番号が並ぶため(一斉配信と同じ方針)。
export const dynamic = "force-dynamic";

export default async function FacilityListPage() {
  await requireRole("editor");
  return <FacilityListPageClient />;
}
