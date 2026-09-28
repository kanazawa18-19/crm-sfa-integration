import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/auth";
import MergePanel from "./merge-panel";

export default async function RecordMergePage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (!user.isManager) return <p className="p-6">マネージャーだけが操作できます。</p>;
  return <div className="space-y-5 p-6">
    <h1 className="text-xl font-bold">重複の比較・統合</h1>
    <p>1組ずつ確認します。残す値を選び、本文と関連の保持を確認してから、元のNotionページをアーカイブします。</p>
    <p>Zoho・kintoneの元レコードは残し、旧IDを統合先へ対応付けます。確認対象は管理中の6データベースです。添付や対応できない本文がある場合は、元を残して保留します。</p>
    <MergePanel />
  </div>;
}
