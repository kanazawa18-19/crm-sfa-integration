import {redirect} from "next/navigation";
import {getCurrentUser} from "@/lib/auth";
import {listRecordExclusions} from "@/lib/backend";
import ExclusionList from "./exclusion-list";
export const dynamic = "force-dynamic";
export default async function Page() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (!user.isManager) return <p>マネージャーだけが確認できます。</p>;
  const result = await listRecordExclusions(user.id);
  return <div className="p-6 space-y-4"><h1 className="text-xl font-bold">同期対象外のお知らせ</h1>
    <p>本人の回答で確定した kintone 取引先2件です。読み書きと自動再作成を停止し、外部レコードは残しています。他ツールの同期は継続します。</p>
    <ExclusionList items={result.items}/></div>;
}
