import {redirect} from "next/navigation";
import {getCurrentUser} from "@/lib/auth";
import prisma from "@/lib/prisma";
import Link from "next/link";
export const dynamic = "force-dynamic";
const phases: Record<string, string> = {project:"案件を収集中", action:"アクションを収集中", ready:"全件収集済み・配信待ち", done:"配信受付と記録が完了", held:"個別確認が必要"};
export default async function Page({searchParams}: {searchParams: Promise<{page?: string; pending?: string}>}) {
  const user=await getCurrentUser(); if(!user) redirect("/login");
  if(!user.isManager) return <p>マネージャーだけが確認できます。</p>;
  const params = await searchParams;
  const page = Math.min(10000, Math.max(0, Number.parseInt(params.page ?? "0", 10) || 0));
  const pending = params.pending === "1";
  const results=await prisma.reportCollection.findMany({where: pending ? {phase: {not: "done"}} : {}, orderBy:{reportDate:"desc"},skip:page*30,take:31,include:{deliveries:true,_count:{select:{pages:true}}}});
  const jobs = results.slice(0, 30);
  const href = (index: number) => `?page=${index}${pending ? "&pending=1" : ""}`;
  return <div className="p-6 space-y-4"><h1 className="text-xl font-bold">日報・週報の収集と配信</h1>
    <p>案件・アクションを全件収集してから配信します。時間内に終わらない場合は10分ごとに続きから進みます。「受付完了」は送信先APIの成功応答を保存した状態です。</p>
    <p>送達記録が未確定の場合は二重送信を避けて停止します。Slack の実物を確認するまで再送しません。</p>
    <details><summary>保留になった日報の確認手順</summary><ol className="list-decimal pl-6">
      <li>対象日と日報・週報の種類を確認し、配信先の Slack で同じ報告を探します。</li>
      <li>見つかった場合はメッセージのリンクを保存してください。見つからない場合も、未送信とは断定せず再送を止めたままにします。</li>
      <li>対象日・種類・確認した配信先・メッセージのリンクまたは未確認の理由を、システム運用担当へ渡してください。送達記録を個別に照合してから再開します。</li>
    </ol></details>
    <nav className="flex gap-4"><Link href="?page=0">すべての実行</Link><Link href="?pending=1">未完了だけ表示（過去分も含む）</Link></nav>
    <table className="w-full text-left"><thead><tr><th>対象日</th><th>収集・配信状態</th><th>収集済み件数</th><th>最終更新</th><th>送達記録</th></tr></thead><tbody>{jobs.map(job=><tr key={job.reportDate.toISOString()} className="border-t">
      <th>{job.reportDate.toISOString().slice(0,10)}</th><td>{phases[job.phase]??job.phase}{job.error&&<p>{job.error}</p>}</td><td>{job._count.pages}</td><td>{job.updatedAt.toLocaleString("ja-JP",{timeZone:"Asia/Tokyo"})}</td>
      <td>{job.deliveries.length?job.deliveries.map(item=><p key={item.kind}>{item.kind==="daily"?"日報":"週報"}: {item.state==="delivered"?"受付完了":"未確定・実物確認が必要"}{item.deliveredAt?`（${item.deliveredAt.toLocaleString("ja-JP",{timeZone:"Asia/Tokyo"})}）`:""}</p>):"未送信"}</td>
    </tr>)}</tbody></table>{jobs.length===0&&<p>実行記録はまだありません。</p>}
    <nav className="flex gap-4">{page > 0 && <Link href={href(page-1)}>前へ</Link>}<span>{page+1}ページ</span>{results.length > 30 && <Link href={href(page+1)}>次へ</Link>}</nav>
  </div>;
}
