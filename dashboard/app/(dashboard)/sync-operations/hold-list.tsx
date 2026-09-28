"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import type { ProductHold } from "@/lib/backend";

const reasons: Record<string, string> = {
  DELIVERY_NOT_VISIBLE: "Notionの関連が確認できず、送り先への配送を保留しています。商品・取引先の関連を確認してください。",
  RELATION_LIMIT_100: "関連先が一度に扱える上限に達しています。取引先に結び付く商品の整理が必要です。",
  MAPPING_MISSING: "同期先の対応が未登録です。対象の同期登録を完了してから再開してください。",
  INVALID_RELATION: "関連先を正しく読み取れません。Notionの関連項目と閲覧権限を確認してください。",
  PAGE_ARCHIVED: "対象ページがアーカイブされています。対象が正しいか確認してください。",
  PAGE_NOT_FOUND: "対象ページが見つかりません。ページと連携の閲覧権限を確認してください。",
  DATABASE_MISMATCH: "関連先が想定したデータベースと異なります。正しいページを選択してください。",
  INVALID_STATUS: "案件の営業状況を確認できません。案件の営業ステータスを確認してください。",
  INVERSE_NOT_VISIBLE: "商品側で取引先の関連を確認できません。両側の関連を確認してください。",
  NOTION_READ_FAILED: "Notionを読み取れません。接続状態と権限を確認してください。",
  NOTION_WRITE_FAILED: "Notionへ関連を保存できません。接続状態と編集権限を確認してください。",
  DELIVERY_FAILED: "送り先への配送が失敗しました。接続状態を確認して再開してください。",
};
function reason(code: string | null) { return reasons[code ?? ""] ?? "原因の確認が必要です。詳細を管理者に共有してください。"; }
function PageLink({ id, label }: { id: string; label: string }) {
  return <a className="underline" href={`https://www.notion.so/${id.replaceAll("-", "")}`} target="_blank" rel="noreferrer">{label}</a>;
}

export default function HoldList({ items }: { items: ProductHold[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function resume(item: ProductHold) {
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await fetch("/api/sync-operations", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: item.projectId, expected_hash: item.hash }) });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 409) { setSelected(null); router.refresh(); }
        throw new Error(data.detail ?? "再開できませんでした");
      }
      setSelected(null); setMessage("再開を予約しました。配送完了ではありません。定期処理で順に配送します。"); router.refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "再開できませんでした"); }
    finally { setBusy(false); }
  }
  return <div className="space-y-4">
    <p aria-live="polite">{message}</p>
    {error && <p role="alert" className="text-red-700">{error}</p>}
    {!items.length && <p>保留中の案件はありません。</p>}
    {items.map(item => <section key={item.projectId} className="space-y-3 rounded border p-4">
      <a className="underline" href={`https://www.notion.so/${item.projectId.replaceAll("-", "")}`} target="_blank" rel="noreferrer">案件を開く</a>
      {item.snapshot.task.evaluationHeld && <p>判定保留: {reason(item.snapshot.task.lastError)}</p>}
      <ul>{item.snapshot.pairs.filter(row => row.state === "held").map(row => <li key={`${row.productId}:${row.clientId}`}><PageLink id={row.productId} label="商品" /> / <PageLink id={row.clientId} label="取引先" />: {reason(row.lastError)}</li>)}</ul>
      <ul>{item.snapshot.deliveries.filter(row => row.state === "held").map(row => <li key={`${row.dbKey}:${row.notionId}`}><PageLink id={row.notionId} label={row.dbKey === "product" ? "商品" : "取引先"} />: {reason(row.lastError)}</li>)}</ul>
      <details><summary>管理者向けの詳細</summary><pre className="whitespace-pre-wrap break-all">{JSON.stringify(item.snapshot, null, 2)}</pre></details>
      {selected === item.projectId ? <div className="space-x-3"><span>この案件の保留を解除し、定期配送を再開します。</span><button disabled={busy} className="rounded border px-3 py-2" onClick={() => resume(item)}>確認して再開</button><button disabled={busy} onClick={() => setSelected(null)}>戻る</button></div>
        : <button disabled={busy} className="rounded border px-3 py-2" onClick={() => setSelected(item.projectId)}>再開内容を確認</button>}
    </section>)}
  </div>;
}
