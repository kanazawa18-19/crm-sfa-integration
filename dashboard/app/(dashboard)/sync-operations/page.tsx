import Link from "next/link";
import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/auth";
import { listProductHolds } from "@/lib/backend";
import prisma from "@/lib/prisma";
import HoldList from "./hold-list";

export const dynamic = "force-dynamic";
export default async function SyncOperationsPage({ searchParams }: {
  searchParams: Promise<{ page?: string }>;
}) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (!user.isManager) return <p className="p-6">マネージャーだけが確認できる画面です。</p>;
  const params = await searchParams;
  const page = Math.min(10000, Math.max(1, Number.parseInt(params.page ?? "1", 10) || 1));
  const [result, history] = await Promise.all([listProductHolds(user.id, (page - 1) * 50), prisma.syncOperationHistory.findMany({
    where: { kind: "product_link_resume" }, orderBy: [{ createdAt: "desc" }, { id: "desc" }], take: 20,
  })]);
  const actors = await prisma.user.findMany({ where: { id: { in: history.map(row => row.actorId) } }, select: { id: true, name: true } });
  const names = new Map(actors.map(actor => [actor.id, actor.name]));
  return <div className="space-y-5 p-6">
    <h1 className="text-xl font-bold">商品関連の保留</h1>
    <p>原因を修正した案件だけ再開してください。再開後、定期処理が現在の商品・取引先を再確認して関連を配送します。</p>
    <HoldList items={result.items} />
    <nav className="flex gap-4">{page > 1 && <Link href={`?page=${page - 1}`}>前へ</Link>}<span>{page}ページ</span>{result.hasMore && <Link href={`?page=${page + 1}`}>次へ</Link>}</nav>
    <h2 className="font-bold">最近の再開履歴（20件）</h2>
    <ul>{history.map(row => <li key={row.id}>{row.createdAt.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" })} — {names.get(row.actorId) ?? "登録者不明"} — 案件 {row.subjectId}</li>)}</ul>
  </div>;
}
