"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export type ReviewItem = {
  id: string; dbKey: string; notionKey: string; propertyName: string;
  sourceTool: string; state: string; revision: number; lastError: string | null;
  history: { action: string; revision: number; actorName: string; time: string }[];
  snapshot: Record<string, { supported: boolean; value?: unknown; canonical?: unknown }>;
};
const stateNames: Record<string, string> = {
  done: "処理完了", superseded: "元ツールで再入力済み",
  pending: "1回目の確認待ち", confirmed: "最終確認待ち", approved: "配送待ち",
  restore_requested: "復元待ち", kept_blank: "却下・空欄維持", failed: "途中で保留",
};
const actionNames: Record<string, string> = { confirm: "1回目確認", approve: "最終承認", restore: "却下・元値復元", keep_blank: "却下・空欄維持", resume: "再開", recheck: "現在値で再確認" };
const toolNames: Record<string, string> = { notion: "Notion", zoho: "Zoho", kintone: "kintone", spreadsheet: "シート" };
const blank = (value: unknown) => value == null || value === "" || (Array.isArray(value) && value.length === 0);
const display = (value: unknown) => blank(value) ? "（空欄）" : typeof value === "string" ? value : JSON.stringify(value);

export default function ReviewList({ items }: { items: ReviewItem[] }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const router = useRouter();
  async function act(item: ReviewItem, action: string, restoreFrom?: string) {
    setBusy(item.id); setError("");
    try {
      const response = await fetch("/api/sync-review", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: item.id, action, revision: item.revision, restore_from: restoreFrom }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "処理に失敗しました");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "処理に失敗しました"); }
    finally { setBusy(null); router.refresh(); }
  }
  if (items.length === 0) return <p>確認待ちはありません。</p>;
  return <div className="space-y-4">
    {error ? <p role="alert" className="rounded border border-red-300 p-3 text-red-700">{error}</p> : null}
    {items.map(item => <section key={item.id} className="space-y-3 rounded-lg border p-4">
      <h2 className="font-semibold">{item.propertyName} — {stateNames[item.state] ?? item.state}</h2>
      <p><a className="underline" href={`https://www.notion.so/${item.notionKey.replaceAll("-", "")}`} target="_blank" rel="noreferrer">対象ページを開く</a>・空欄にしたツール：{toolNames[item.sourceTool] ?? item.sourceTool}</p>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">ツール</th><th className="p-2">確認時の値</th></tr></thead>
        <tbody>{Object.entries(item.snapshot).map(([tool, value]) => <tr key={tool} className="border-t">
          <td className="p-2">{toolNames[tool] ?? tool}</td><td className="whitespace-pre-wrap break-all p-2">{value.supported ? display(value.value) : "対応項目の確認が必要"}</td>
        </tr>)}</tbody></table></div>
      {["pending", "confirmed", "kept_blank"].includes(item.state) ? <p className="text-sm">「同期を保留」では元ツールの空欄と他ツールの値をそれぞれ維持し、この項目の変更は配送されません。元ツールに値を入れると解除します。</p> : null}
      {item.lastError ? <p role="status">保留理由：{item.lastError}</p> : null}
      {item.state === "confirmed" ? <p className="font-semibold text-red-700">本当に削除してよいですか？ 他のツールのこの項目も空欄になります。</p> : null}
      <div className="flex flex-wrap gap-3">
        {item.state === "pending" ? <button disabled={busy !== null} className="rounded border px-3 py-2" onClick={() => act(item, "confirm")}>空欄にされた項目の削除を確認する</button> : null}
        {item.state === "confirmed" ? <button disabled={busy !== null} className="rounded bg-red-700 px-3 py-2 text-white" onClick={() => act(item, "approve")}>最終承認して削除する</button> : null}
        {["pending", "confirmed"].includes(item.state) ? <>
          <button disabled={busy !== null} className="rounded border px-3 py-2" onClick={() => act(item, "keep_blank")}>却下して、この項目の同期を保留</button>
          {Object.entries(item.snapshot).filter(([, value]) => value.canonical !== undefined && !blank(value.canonical)).map(([tool, value]) =>
            <button key={tool} disabled={busy !== null} className="rounded border px-3 py-2" title={display(value.canonical)} onClick={() => act(item, "restore", tool)}>却下して{toolNames[tool] ?? tool}の元値へ戻す</button>)}
        </> : null}
        {["failed", "approved", "restore_requested"].includes(item.state) ? <button disabled={busy !== null} className="rounded border px-3 py-2" onClick={() => act(item, "resume")}>現在値を再確認して再開</button> : null}
        {["failed", "kept_blank"].includes(item.state) ? <button disabled={busy !== null} className="rounded border px-3 py-2" onClick={() => act(item, "recheck")}>現在値を読み直し、1回目から確認する</button> : null}
      </div>
      {item.state === "failed" ? <p className="text-sm">現在値で再確認すると、途中で配送した値はそのままに、新しい差分で承認をやり直します。以前の値は履歴に残ります。</p> : null}
      <details><summary className="cursor-pointer">判断履歴（最新20件）</summary>
        {item.history.length ? <ul className="mt-2 space-y-1 text-sm">{item.history.map(entry =>
          <li key={entry.revision}>{entry.time} — {entry.actorName}：{actionNames[entry.action] ?? entry.action}</li>)}</ul> : <p>まだ判断されていません。</p>}
      </details>
      {busy === item.id ? <p aria-live="polite">処理中です…</p> : null}
    </section>)}
  </div>;
}
