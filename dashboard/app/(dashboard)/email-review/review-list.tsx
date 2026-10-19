"use client";

import { useCallback, useEffect, useState } from "react";

type Option = { id: string; name: string };
type Row = {
  id: string; revision: number; subject: string; reason: string; state: string; delivery: string;
  direction: string; sent_evidence?: boolean; correction_active?: boolean; owner: string; activity_owner?: string; gmail_id: string; channel?: string;
  participants: { name: string; email: string }[]; contacts: Option[]; candidates: Option[];
  evidence: { source: string; value: string; message_id: string }[];
  company_id?: string; company_name?: string; contact_id?: string; target_id?: string; target_name?: string; project_id?: string; project_name?: string; action_id?: string;
};
const labels: Record<string, string> = { new: "照合待ち", evaluating: "照合中", pending: "確認待ち", held: "保留", excluded: "対象外", approved: "関連先確定・処理待ち", processing: "記録中", done: "記録作成済み", unknown: "登録結果の照合待ち", recheck: "結果照合待ち", reconciling: "結果照合中", not_started: "未配送", queued: "配送待ち", delivered: "配送処理完了", partial: "一部未反映" };

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: "no-store", ...init });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "処理に失敗しました");
  return body;
}

function SearchChoice({ label, rowId, kind, value, onChange, initial }: {
  label: string; rowId: string; kind: string; value: string; onChange: (id: string) => void; initial: Option[];
}) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function search() {
    setBusy(true); setError("");
    try {
      const args = new URLSearchParams({ id: rowId, kind, query });
      const result = await request<{ rows: Option[] }>(`/api/email-review?${args}`);
      setOptions(previous => [...new Map([...previous.filter(item => item.id === value), ...result.rows].map(item => [item.id, item])).values()]);
      if (!result.rows.length) setError("候補はありません。検索語を変えてください");
    } catch (e) { setError(e instanceof Error ? e.message : "検索に失敗しました"); }
    finally { setBusy(false); }
  }
  return <fieldset className="space-y-2 min-w-0">
    <legend className="font-semibold">{label}</legend>
    <div className="flex gap-2"><input className="input min-w-0 flex-1" aria-label={`${label}の検索語`} value={query} maxLength={80} onChange={e => setQuery(e.target.value)} placeholder="2文字以上で検索" />
      <button type="button" className="btn-ghost" disabled={busy || query.trim().length < 2} onClick={search}>{busy ? "検索中…" : "検索"}</button></div>
    <select className="input w-full" aria-label={label} value={value} onChange={e => onChange(e.target.value)}>
      <option value="">選択しない</option>
      {value && !options.some(o => o.id === value) && <option value={value}>現在の選択（{value.slice(-8)}）</option>}
      {options.map(option => <option key={option.id} value={option.id}>{option.name}（{option.id.slice(-8)}）</option>)}
    </select>
    {error && <p role="status">{error}</p>}
  </fieldset>;
}

export function Card({ row, reload }: { row: Row; reload: () => void }) {
  const [company, setCompany] = useState(row.company_id ?? "");
  const [contact, setContact] = useState(row.contact_id ?? (row.contacts.length === 1 ? row.contacts[0].id : ""));
  const [target, setTarget] = useState(row.target_id ?? "");
  const [project, setProject] = useState(row.project_id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function decide(action: string) {
    setBusy(true); setError("");
    try {
      await request("/api/email-review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        id: row.id, revision: row.revision, action, company_id: company || null, contact_id: contact || null, target_id: target || null, project_id: project || null,
      }) });
      reload();
    } catch (e) { setError(e instanceof Error ? e.message : "保存できませんでした"); }
    finally { setBusy(false); }
  }
  const correctionActive = row.correction_active === true;
  const sent = row.direction === "outbound" && row.sent_evidence === true;
  const blocked = ["processing", "unknown", "evaluating", "recheck", "reconciling"].includes(row.state);
  return <article className="surface-card p-4 sm:p-6 space-y-4 min-w-0">
    <h2 className="font-semibold break-words">{row.subject}</h2>
    <p className="break-all">{row.direction === "outbound" ? (sent ? "送信" : "送信状態未確認") : "受信"}：{row.participants.map(p => `${p.name} ${p.email}`).join("、")}</p>
    <p>{labels[row.state] ?? row.state} ／ {labels[row.delivery] ?? row.delivery}</p>
    <p>{row.reason}{row.channel === "slack" ? "（Slackで受付）" : row.channel === "automatic" ? "（自動照合）" : ""}</p>
    <p className="break-all">記録担当者：{row.activity_owner ?? row.owner}{row.direction === "inbound" ? "（確定した本人を記録します）" : ""}</p>
    <a className="link" target="_blank" rel="noreferrer" href={`https://mail.google.com/mail/?authuser=${encodeURIComponent(row.owner)}#all/${encodeURIComponent(row.gmail_id)}`}>元メールを見る（別タブ）</a>
    <details><summary className="cursor-pointer underline">照合根拠を見る（{row.evidence.length}件）</summary>
      <dl className="mt-2 space-y-2">{row.evidence.map((item, index) => <div key={index}><dt className="font-semibold">{item.source === "salutation" ? "宛名" : "相手の署名"}</dt><dd className="break-words">{item.value}</dd></div>)}</dl>
    </details>
    {correctionActive && row.state !== "excluded" && <p role="status">訂正処理中です。通常の操作は停止しています。管理者に専用手順での再開を依頼してください。</p>}
    {!sent && !correctionActive && <p role="status">送信済みと確認できないため、登録・訂正・再照合はできません。下書きは対象外です。送信済みメールの場合は管理者に元メールの状態の再取得を依頼し、その後「更新」で確認してください。</p>}
    {sent && !correctionActive && <fieldset disabled={busy || blocked} className="space-y-4 min-w-0">
      <legend className="font-semibold">関連先を確認・訂正</legend>
      <label className="block">相手の連絡先<select className="input w-full mt-1" value={contact} onChange={e => setContact(e.target.value)}><option value="">選んでください</option>{row.contacts.map(c => <option key={c.id} value={c.id}>{c.name}（{c.id.slice(-8)}）</option>)}</select></label>
      {!row.contacts.length && <p>連絡先が未登録です。上記の相手メールアドレスでCRMに連絡先を登録してから「再照合」を押してください。</p>}
      <SearchChoice label="相手の所属会社" rowId={row.id} kind="company" value={company} onChange={setCompany} initial={row.candidates} />
      <SearchChoice label="今回の対象施設（未選択なら所属会社）" rowId={row.id} kind="company" value={target} onChange={value => { setTarget(value); setProject(""); }} initial={[...row.candidates.filter(c => c.id !== row.target_id), ...(row.target_id && row.target_name ? [{id: row.target_id, name: row.target_name}] : [])]} />
      <SearchChoice label="今回の案件（任意）" rowId={row.id} kind="project" value={project} onChange={setProject} initial={row.project_id && row.project_name ? [{id: row.project_id, name: row.project_name}] : []} />
      <div className="flex flex-wrap gap-2">
        <button className="btn-primary" disabled={!company || !contact} onClick={() => decide("confirm")}>{row.action_id ? "関連先を訂正" : "関連先を確定"}</button>
        {!row.action_id && <><button className="btn-ghost" onClick={() => decide("hold")}>保留</button><button className="btn-ghost" onClick={() => decide("exclude")}>営業記録の対象外</button></>}
        <button className="btn-ghost" onClick={() => decide("resume")}>再照合</button>
      </div>
    </fieldset>}
    {sent && !correctionActive && row.state === "unknown" && <button disabled={busy} className="btn-primary" onClick={() => decide("reconcile")}>登録済みの記録を探す（再登録しません）</button>}
    {busy && <p aria-live="polite">受付中…</p>}
    {error && <div role="alert">{error}<button className="btn-ghost" onClick={reload}>最新の状態を読み直す</button></div>}
  </article>;
}

export default function ReviewList() {
  const [state, setState] = useState("pending");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ enabled: boolean; rows: Row[] } | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion(v => v + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    request<{ enabled: boolean; rows: Row[] }>(`/api/email-review?state=${state}&offset=${offset}`, { signal: controller.signal })
      .then(result => { setData(result); setError(""); }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [state, offset, version]);
  return <div className="space-y-4">
    <div className="flex flex-wrap gap-2" aria-label="表示する状態">
      {[["pending", "確認待ち"], ["held", "保留"], ["approved", "確定・処理状況"], ["excluded", "対象外"]].map(([key, label]) => <button key={key} aria-pressed={state === key} className={state === key ? "btn-primary" : "btn-ghost"} onClick={() => { setData(null); setState(key); setOffset(0); reload(); }}>{label}</button>)}
      <button className="btn-ghost" onClick={reload}>更新</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {!data && !error && <p aria-live="polite">読込み中…</p>}
    {data && !data.enabled && <p>メール照合は準備中です。本番での取込みは開始していません。</p>}
    {data?.enabled && !data.rows.length && <p>この状態のメールはありません。</p>}
    {data?.rows.map(row => <Card key={`${row.id}:${row.revision}`} row={row} reload={reload} />)}
    <div className="flex gap-2"><button className="btn-ghost" disabled={offset === 0} onClick={() => { setData(null); setOffset(v => Math.max(0, v - 30)); }}>前へ</button><button className="btn-ghost" disabled={data?.rows.length !== 30} onClick={() => { setData(null); setOffset(v => v + 30); }}>次へ</button></div>
  </div>;
}
