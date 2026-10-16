import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/auth";
import ReviewList from "./review-list";

export default async function EmailReviewPage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return <div className="space-y-6">
    <h1 className="page-title">メールの照合</h1>
    <p>自分のメールについて、相手の所属と今回の対象を確認します。宛名と相手の署名に同じ会社名があれば、2つの根拠として自動照合します。</p>
    <ReviewList />
  </div>;
}
