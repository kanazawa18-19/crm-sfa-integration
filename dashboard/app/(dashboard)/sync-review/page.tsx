import Link from "next/link";
import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/auth";
import prisma from "@/lib/prisma";
import ReviewList, { type ReviewItem } from "./review-list";

export const dynamic = "force-dynamic";
const closedStates = ["done", "superseded", "kept_blank"];

export default async function SyncReviewPage({ searchParams }: {
  searchParams: Promise<{ view?: string; page?: string }>;
}) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (!user.isManager) return <p className="p-6">マネージャーだけが確認できる画面です。</p>;
  const params = await searchParams;
  const history = params.view === "history";
  const page = Math.min(10000, Math.max(1, Number.parseInt(params.page ?? "1", 10) || 1));
  const rows = await prisma.syncFieldReview.findMany({
    where: { state: history ? { in: closedStates } : { notIn: closedStates } },
    orderBy: [{ createdAt: history ? "desc" : "asc" }, { id: "asc" }], skip: (page - 1) * 50, take: 51,
    select: { id: true, dbKey: true, notionKey: true, propertyName: true,
      sourceTool: true, snapshot: true, state: true, revision: true, lastError: true,
      history: { orderBy: { createdAt: "desc" }, take: 20,
        select: { actorId: true, action: true, revision: true, createdAt: true } } },
  });
  const displayed = rows.slice(0, 50);
  const actorIds = [...new Set(displayed.flatMap(row => row.history.map(entry => entry.actorId)))];
  const actors = await prisma.user.findMany({ where: { id: { in: actorIds } }, select: { id: true, name: true } });
  const names = new Map(actors.map(actor => [actor.id, actor.name]));
  const items = displayed.map(row => ({ ...row, history: row.history.map(entry => ({
    action: entry.action, revision: entry.revision, actorName: names.get(entry.actorId) ?? "担当マネージャー",
    time: entry.createdAt.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" }),
  })) }));
  const view = history ? "history" : "pending";
  return <main className="space-y-6 p-6">
    <h1 className="text-2xl font-bold">同期の確認待ち</h1>
    <p>空欄に変更された項目は、2回の承認が済むまで他のツールから消しません。対象ごとに、削除・元値へ戻す・空欄を維持する、を選べます。</p>
    <nav className="flex gap-4"><Link className="underline" href="/sync-review">処理待ち</Link><Link className="underline" href="/sync-review?view=history">処理済み・空欄維持の履歴</Link></nav>
    <ReviewList items={items as unknown as ReviewItem[]} />
    <nav className="flex gap-4" aria-label="確認一覧のページ">
      {page > 1 ? <Link className="underline" href={`/sync-review?view=${view}&page=${page - 1}`}>前の50件</Link> : null}
      <span>{page}ページ目</span>
      {rows.length > 50 ? <Link className="underline" href={`/sync-review?view=${view}&page=${page + 1}`}>次の50件</Link> : null}
    </nav>
  </main>;
}
