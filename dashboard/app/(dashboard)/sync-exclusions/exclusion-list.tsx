"use client";
import {useState} from "react";
import {useRouter} from "next/navigation";
import type {RecordExclusion} from "@/lib/backend";
export default function ExclusionList({items}: {items: RecordExclusion[]}) {
  const router = useRouter(); const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  async function acknowledge(id: string) {
    setBusy(true); setMessage("");
    try {
      const response = await fetch("/api/sync-exclusions", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({external_id: id})});
      const body = await response.json(); if (!response.ok) throw new Error(body.detail ?? "記録できません");
      setMessage("確認済みを記録しました。対象外の設定は維持されます。"); router.refresh();
    } catch(error) {setMessage(error instanceof Error ? error.message : "記録できません");}
    finally {setBusy(false);}
  }
  return <><p aria-live="polite">{message}</p><ul>{items.map(item => <li key={item.id} className="border rounded p-4 my-3">
    <strong>kintone 取引先 ID {item.id}</strong><p>{item.reason}（回答 {item.decision}）</p>
    {item.acknowledgement ? <p>確認済み: {new Date(item.acknowledgement.createdAt).toLocaleString("ja-JP")}</p> : <button className="border rounded p-2" disabled={busy} onClick={() => acknowledge(item.id)}>対象外の理由を確認しました</button>}
  </li>)}</ul></>;
}
