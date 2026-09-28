"use client";
import { useEffect, useState } from "react";

type Snapshot = { dbKey: string; sourceId: string; targetId: string; source: Record<string, unknown>;
  target: Record<string, unknown>; unionFields: string[]; children: unknown[]; sourceBlocks: unknown[] };
type Step = { kind: string; dbKey: string; id: string; name?: string; property?: string; before: unknown; desired: unknown };
type Job = { steps?: Step[]; actorName?: string; updatedAt?: string; progress?: Record<string, {state: string}>; history?: {actorName?: string; createdAt: string; afterState: {state?: string; step?: number}}[]; id: string; state: string; planHash: string; sourceId: string; targetId: string; error?: string };
type AliasEvent = { id: string; state: string; sourceTool: string; oldId: string; targetId: string; eventAt: string };
type AliasComparison = { hash: string; comparison: { id: string; targetId: string; old: Record<string, unknown>; notion: Record<string, unknown>; crm: Record<string, unknown> } };
type RelationPreview = { reviewId: string; targetId: string; hash?: string; snapshot?: { source: {dbKey: string; notionId: string; property: string}; targetName: string; before: string[]; desired: string[] }; sourceOptions: {dbKey: string; property: string; field: string; notionId: string}[] };
type CreationCandidate = { id: string; dbKey: string; sourceId: string; tool: string; externalId: string; state: string };
type CreationPreview = { display: {name: string; notion: unknown; external: unknown}[]; hash: string; snapshot: CreationCandidate & {external: Record<string, unknown>; notion: Record<string, unknown>; mappedId?: string} };
type Candidate = { decisionState?: string; id: string; targetDbKey: string; rawValue: string; candidateNotionPageIds: string[]; candidateRawNames: string[] };
const names: Record<string, string> = { client_master: "取引先", chain: "チェーン", contact: "連絡先", project: "案件", product: "商品", action: "アクション" };
const states: Record<string, string> = { draft: "確認待ち", approved: "実行待ち", running: "実行中", held: "保留", dismissed: "見送り", done: "完了", abandoned: "途中結果を保持して中止" };
const candidateStates: Record<string, string> = {pending: "比較待ち", dismissed: "見送り済み", reserved: "取り込み結果の確認待ち", imported: "取り込み済み・統合の比較待ち"};
const stepName = (kind: string) => ({properties: "値の補完・選択", relation: "子の関連変更", append_body: "本文追加", archive: "元のアーカイブ"}[kind] ?? kind);
const show = (value: unknown) => value == null || value === "" ? "（空欄）" : typeof value === "string" ? value : JSON.stringify(value);
const pageId = (value: string) => {
  const match = value.match(/[a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}/i);
  return match ? match[0].replaceAll("-", "").replace(/(.{8})(.{4})(.{4})(.{4})(.{12})/, "$1-$2-$3-$4-$5") : value.trim();
};

export default function MergePanel() {
  const [resetPreview, setResetPreview] = useState<{hash: string; snapshot: {id: string; marker: string}} | null>(null);
  const [resetConfirmed, setResetConfirmed] = useState(false);
  const [importRecovery, setImportRecovery] = useState<{hash: string; snapshot: {id: string; pageId: string; tool: string; externalId: string; properties: Record<string, unknown>}} | null>(null);
  const [abandonPreview, setAbandonPreview] = useState<{hash: string; snapshot: {id: string; actual: {step: Step; current?: unknown; readable: boolean}[]; progress: Record<string, {state: string}>}} | null>(null);
  const [creationCandidates, setCreationCandidates] = useState<CreationCandidate[]>([]);
  const [creationPreview, setCreationPreview] = useState<CreationPreview | null>(null);
  const [db, setDb] = useState("client_master");
  const [source, setSource] = useState(""); const [target, setTarget] = useState("");
  const [comparison, setComparison] = useState<{ snapshot: Snapshot; hash: string } | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Job | null>(null);
  const [relationPreview, setRelationPreview] = useState<RelationPreview | null>(null);
  const [relationTargets, setRelationTargets] = useState<Record<string, string>>({});
  const [aliasEvents, setAliasEvents] = useState<AliasEvent[]>([]);
  const [aliasComparison, setAliasComparison] = useState<AliasComparison | null>(null);
  const [page, setPage] = useState(0); const [more, setMore] = useState(false);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]); const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  async function call(body: Record<string, unknown>) {
    const response = await fetch("/api/record-merge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "処理を確認できません");
    return data;
  }
  async function refresh() { const data = await call({ action: "list", offset: page * 50 }); setCreationCandidates(data.creationCandidates ?? []); setJobs(data.items); setCandidates(data.candidates); setMore(data.hasMore || data.hasMoreCandidates || data.hasMoreAliasEvents || data.hasMoreCreationCandidates); setAliasEvents(data.aliasEvents); }
  useEffect(() => {
    let active = true;
    void call({ action: "list", offset: page * 50 }).then(data => {
      if (!active) return;
      setCreationCandidates(data.creationCandidates ?? []); setJobs(data.items);
      setCandidates(data.candidates); setAliasEvents(data.aliasEvents);
      setMore(data.hasMore || data.hasMoreCandidates || data.hasMoreAliasEvents || data.hasMoreCreationCandidates);
    }).catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [page]);
  async function run(action: () => Promise<void>) {
    setBusy(true); setMessage("");
    let succeeded = false;
    try { await action(); succeeded = true; }
    catch (error) { setMessage(error instanceof Error ? error.message : "処理を確認できません"); }
    finally {
      await refresh().catch(() => setMessage(previous => `${previous} ${succeeded ? "操作は完了しましたが、" : ""}一覧を取得できません。再実行せず、画面を読み直して履歴を確認してください。`));
      setBusy(false);
    }
  }
  const snapshot = comparison?.snapshot;
  const fields = snapshot ? Array.from(new Set([...Object.keys(snapshot.source), ...Object.keys(snapshot.target)])).sort() : [];
  return <div className="space-y-5">
    <h2 className="font-bold">外部登録で見つかった候補</h2>
    <p>既存候補を確認してから統合または見送りを選びます。見送りは確認した内容だけに有効です。変更があれば再確認します。</p>
    <ul>{creationCandidates.map(item => <li key={item.id} className="border rounded p-3 my-2">{names[item.dbKey]}・{item.tool}・ID {item.externalId}（{candidateStates[item.state] ?? item.state}）
      <button disabled={busy} className="border ml-3 px-2" onClick={() => run(async () => { setCreationPreview(await call({action: "compare_creation", operation_id: item.id})); })}>現在値を比較</button>
      {item.state === "reserved" && <label className="block">取り込みで作成された実物の Notion リンク<input className="border mx-2" value={relationTargets[item.id] ?? ""} onChange={e => setRelationTargets({...relationTargets, [item.id]: e.target.value})}/><button disabled={busy || !relationTargets[item.id]} className="border px-2" onClick={() => run(async () => { setImportRecovery(await call({action: "compare_import_recovery", operation_id: item.id, target_id: pageId(relationTargets[item.id])})); })}>外部識別子と取り込み内容を照合</button></label>}
      {item.state === "reserved" && <button disabled={busy} className="border px-2" onClick={() => run(async () => { setResetConfirmed(false); setResetPreview(await call({action: "preview_reset_import", operation_id: item.id})); })}>ページが作成されていない場合の確認へ</button>}
    </li>)}</ul>
    {resetPreview && <div className="border border-amber-500 p-4 space-y-3">
      <h3 className="font-bold">未作成を確認して取り込み予約を解除</h3>
      <p>次の確認番号を持つページが Notion とごみ箱の両方にないことを実物で確認してください。検索できない・結果が不明な場合は解除しないでください。作成済みなのに解除すると、次の取り込みで重複ページができます。</p>
      <p>{resetPreview.snapshot.marker}</p>
      <label><input type="checkbox" checked={resetConfirmed} onChange={e => setResetConfirmed(e.target.checked)}/>Notionとごみ箱で未作成を確認しました</label>
      <button disabled={busy || !resetConfirmed} className="border px-3 py-2" onClick={() => run(async () => { await call({action: "reset_import", operation_id: resetPreview.snapshot.id, expected_hash: resetPreview.hash, confirmation: "Notionとごみ箱で未作成を確認しました"}); setResetPreview(null); setMessage("未作成の確認を記録し、予約を解除しました。ページは作成していません。現在値の比較からやり直せます。"); })}>未作成の確認を記録して予約だけ解除</button>
    </div>}
    {importRecovery && <div className="border border-amber-500 p-4 space-y-3"><h3 className="font-bold">実物の取り込み結果を確定する前の確認</h3>
      <p>{importRecovery.snapshot.tool} の外部 ID {importRecovery.snapshot.externalId} と、取り込み時の確認番号・内容が一致しました。新規作成はせず、このページの対応表だけを確定します。</p>
      <a className="underline" href={`https://www.notion.so/${importRecovery.snapshot.pageId.replaceAll("-", "")}`} target="_blank" rel="noreferrer">照合した実物ページ</a>
      <table><tbody>{Object.entries(importRecovery.snapshot.properties).map(([name,value])=><tr key={name}><th>{name}</th><td>{show(value)}</td></tr>)}</tbody></table>
      <button disabled={busy} className="border px-3 py-2" onClick={() => run(async () => { await call({action: "recover_import", operation_id: importRecovery.snapshot.id, target_id: importRecovery.snapshot.pageId, expected_hash: importRecovery.hash}); setImportRecovery(null); setMessage("実物の取り込み結果を確定しました。候補比較から統合へ進めます。"); })}>照合したページを取り込み結果として確定</button></div>}
    {creationPreview && <div className="border border-amber-500 p-4 space-y-3">
      <h3 className="font-bold">外部候補の実行前確認</h3>
      <table className="w-full text-left"><thead><tr><th>項目</th><th>登録元 Notion</th><th>外部候補の現在値</th></tr></thead><tbody>{creationPreview.display.map(item => <tr key={item.name} className="border-t"><th>{item.name}</th><td>{show(item.notion)}</td><td>{show(item.external)}</td></tr>)}</tbody></table>
      <details><summary>全項目の詳細（未対応項目も含む）</summary><div className="grid md:grid-cols-2 gap-3"><div>登録元 Notion<pre className="overflow-auto text-xs">{JSON.stringify(creationPreview.snapshot.notion, null, 2)}</pre></div><div>既存の外部レコード<pre className="overflow-auto text-xs">{JSON.stringify(creationPreview.snapshot.external, null, 2)}</pre></div></div></details>
      <p>統合へ進む場合、未取り込みの外部レコードだけを Notion に取り込みます。外部レコードは作成しません。続く比較画面で残す値を選び、別途統合を承認します。</p>
      <button disabled={busy} className="border rounded px-3 py-2" onClick={() => run(async () => { const result = await call({action: "import_creation", operation_id: creationPreview.snapshot.id, expected_hash: creationPreview.hash}); setDb(result.dbKey); setSource(result.sourceId); setTarget(result.targetId); setComparison(null); setDraft(null); setCreationPreview(null); setMessage("比較する2件を設定しました。下の比較操作へ進んでください。"); })}>既存外部レコードを取り込み、統合の比較へ進む</button>
      <button disabled={busy || ["reserved", "imported"].includes(creationPreview.snapshot.state)} className="border rounded px-3 py-2 ml-2" onClick={() => run(async () => { await call({action: "dismiss_creation", operation_id: creationPreview.snapshot.id, expected_hash: creationPreview.hash}); setCreationPreview(null); setMessage("この内容の候補を見送りました。登録元を再通知すると新規登録の確認を再開します。kintone 顧客名の重複禁止は維持されます。"); })}>別件と判断し、この候補を見送る</button>
    </div>}
    <h2 className="font-bold">旧IDから届いた変更</h2>
    <p>統合前のCRMレコードへの変更は、正本の新しい値を保護するため自動転送しません。反映が必要な項目は正本で編集し、比較してから処理してください。</p>
    <ul>{aliasEvents.map(event => <li key={event.id} className="border rounded p-3 my-2">{event.sourceTool}・旧ID {event.oldId} — {new Date(event.eventAt).toLocaleString("ja-JP")} — {event.state === "pending" ? "比較待ち" : "正本を維持して処理済み"}
      {event.state === "pending" && <button disabled={busy} className="ml-3 border px-2" onClick={() => run(async () => { setAliasComparison(await call({ action: "compare_alias", operation_id: event.id })); })}>現在の正本と比較</button>}
    </li>)}</ul>
    {aliasComparison && <div className="border border-amber-500 p-4">
      <table className="w-full text-left"><thead><tr><th>項目</th><th>旧IDの変更</th><th>現在のNotion</th></tr></thead><tbody>{Object.keys(aliasComparison.comparison.old).map(name => <tr key={name}><th>{name}</th><td>{show(aliasComparison.comparison.old[name])}</td><td>{show(aliasComparison.comparison.notion[name])}</td></tr>)}</tbody></table>
      <details><summary>正本CRMの現在値</summary><pre className="overflow-auto text-xs">{JSON.stringify(aliasComparison.comparison.crm, null, 2)}</pre></details>
      <p>次の操作は旧変更を転送せず、現在の正本を維持して比較待ちを終了します。</p>
      <button disabled={busy} className="border rounded px-3 py-2" onClick={() => run(async () => { await call({ action: "keep_canonical", operation_id: aliasComparison.comparison.id, expected_hash: aliasComparison.hash }); setAliasComparison(null); setMessage("現在の正本を維持した判断を記録しました。"); })}>旧変更を反映せず、正本を維持する</button>
    </div>}
    <h2 className="font-bold">関連先の確認候補</h2>
    <p>同名だけで統合しません。候補から2件を選ぶと比較欄へ入ります。元項目や変更時点が不明な旧保留は、統合後も個別の再確認が必要です。</p>
    <ul>{candidates.map(candidate => <li key={candidate.id} className="rounded border p-3 my-2">
      <strong>{candidate.rawValue}</strong>（{names[candidate.targetDbKey] ?? candidate.targetDbKey}）
      {candidate.decisionState && candidate.decisionState !== "done" && <button disabled={busy} className="ml-3 border px-2" onClick={() => run(async () => { await call({ action: "resume_relation", operation_id: candidate.id }); setMessage("承認済みの関連処理を再開しました。"); })}>承認済みの関連付けを再開</button>}
      <ul>{candidate.candidateNotionPageIds.map((id, index) => <li key={id}>
        <a href={`https://www.notion.so/${id.replaceAll("-", "")}`} target="_blank" rel="noreferrer">{candidate.candidateRawNames[index] ?? "名前を確認"}</a>
        <button disabled={busy} className="ml-3 border px-2" onClick={() => { setDb(candidate.targetDbKey); setSource(id); setComparison(null); setDraft(null); }}>元に選ぶ</button>
        <button disabled={busy} className="ml-3 border px-2" onClick={() => { setDb(candidate.targetDbKey); setTarget(id); setComparison(null); setDraft(null); }}>残す先に選ぶ</button>
        <button disabled={busy || Boolean(candidate.decisionState)} className="ml-3 border px-2" onClick={() => run(async () => { const data = await call({ action: "compare_relation", operation_id: candidate.id, target_id: id }); setRelationPreview({ ...data, reviewId: candidate.id, targetId: id }); })}>この候補への関連付けを比較</button>
      </li>)}</ul>
      {candidate.candidateNotionPageIds.length === 0 && <label>外部IDと対応するNotionのリンク<input className="border mx-2" value={relationTargets[candidate.id] ?? ""} onChange={e => setRelationTargets({ ...relationTargets, [candidate.id]: e.target.value })} /><button disabled={busy || !relationTargets[candidate.id]} className="border px-2" onClick={() => run(async () => { const id = pageId(relationTargets[candidate.id]); const data = await call({ action: "compare_relation", operation_id: candidate.id, target_id: id }); setRelationPreview({ ...data, reviewId: candidate.id, targetId: id }); })}>外部IDと現在値を照合</button></label>}
    </li>)}</ul>
    {relationPreview && <div className="border border-amber-500 p-4 space-y-3">
      <h3 className="font-bold">関連付けの実行前確認</h3>
      {!relationPreview.snapshot ? <><p>元のアプリ・項目を選んでください。実際に現在値が一致した項目だけを表示しています。</p>{relationPreview.sourceOptions.map(option => <button key={option.dbKey + option.property} disabled={busy} className="border p-2 mr-2" onClick={() => run(async () => { const data = await call({ action: "compare_relation", operation_id: relationPreview.reviewId, target_id: relationPreview.targetId, source_db: option.dbKey, property_name: option.property }); setRelationPreview({ ...data, reviewId: relationPreview.reviewId, targetId: relationPreview.targetId }); })}>{names[option.dbKey]}・{option.property}</button>)}</> : <>
        <p>{names[relationPreview.snapshot.source.dbKey]}の「{relationPreview.snapshot.source.property}」を「{relationPreview.snapshot.targetName}」へ関連付けます。</p>
        <p>現在の関連: {show(relationPreview.snapshot.before)} → 実行後: {show(relationPreview.snapshot.desired)}</p>
        <p>選択先を追加し、現在の他の関連は保持します。</p>
        <button disabled={busy} className="border rounded px-3 py-2" onClick={() => run(async () => { const item = relationPreview.snapshot!; await call({ action: "approve_relation", operation_id: relationPreview.reviewId, target_id: relationPreview.targetId, source_db: item.source.dbKey, property_name: item.source.property, expected_hash: relationPreview.hash }); setRelationPreview(null); setMessage("関連付けと判断履歴を保存しました。"); })}>表示した関連への変更を承認して実行</button>
      </>}
    </div>}
    <div className="grid gap-3 md:grid-cols-3">
      <label>種類<select className="block w-full border p-2" value={db} onChange={e => { setDb(e.target.value); setComparison(null); setDraft(null); }}>{Object.entries(names).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
      <label>統合元のNotionリンク・ID<input className="block w-full border p-2" value={source} onChange={e => { setSource(e.target.value); setComparison(null); setDraft(null); }} /></label>
      <label>残す先のNotionリンク・ID<input className="block w-full border p-2" value={target} onChange={e => { setTarget(e.target.value); setComparison(null); setDraft(null); }} /></label>
    </div>
    <button disabled={busy || !source || !target} className="rounded border px-4 py-2 disabled:opacity-50" onClick={() => run(async () => {
      setDraft(null); setChoices({}); setComparison(await call({ action: "compare", db_key: db, source_id: pageId(source), target_id: pageId(target) }));
    })}>現在の内容を比較</button>
    {snapshot && <>
      <div className="overflow-auto"><table className="w-full text-left"><thead><tr><th>項目</th><th>統合元</th><th>残す先</th><th>残す値</th></tr></thead><tbody>{fields.map(name => <tr key={name} className="border-t">
        <th className="p-2">{name}</th><td className="max-w-sm break-words p-2">{show(snapshot.source[name])}</td><td className="max-w-sm break-words p-2">{show(snapshot.target[name])}</td>
        <td>{snapshot.unionFields.includes(name) ? "全件を保持" : [snapshot.source[name], snapshot.target[name]].some(value => value == null || value === "" || (Array.isArray(value) && value.length === 0)) ? "入力済みの値を保持" : JSON.stringify(snapshot.source[name]) === JSON.stringify(snapshot.target[name]) ? "同じ値を保持" : <select aria-label={`${name}の残す値`} value={choices[name] ?? ""} disabled={Boolean(draft)} onChange={e => setChoices({ ...choices, [name]: e.target.value })}><option value="">選んでください</option><option value="source">統合元</option><option value="target">残す先</option></select>}</td>
      </tr>)}</tbody></table></div>
      <p>元の本文 {snapshot.sourceBlocks.length} ブロック、元への参照 {snapshot.children.length} 件を確認しました。複数の関連・選択は全件保持します。外部の単一関連欄は、相違があれば残す値を選びます。</p>
      {!draft && <button disabled={busy} className="rounded border px-4 py-2" onClick={() => run(async () => {
        const selected = Object.fromEntries(Object.entries(choices).filter(([, value]) => value));
        setDraft(await call({ action: "prepare", db_key: db, source_id: snapshot.sourceId, target_id: snapshot.targetId, expected_hash: comparison.hash, choices: selected }));
      })}>この内容で実行前の確認へ</button>}
    </>}
    {draft && <div className="rounded border border-amber-500 p-4 space-y-3">
      <h2 className="font-bold">実行する変更の一覧</h2>
      {draft.steps?.map((step, index) => <div key={index} className="border-b py-2">
        <strong>{index + 1}. {step.kind === "archive" ? "元のページをアーカイブ" : step.kind === "append_body" ? "元の本文を追加" : step.kind === "relation" ? "子の参照を変更" : "残す先の値を確定"}</strong>
        <a className="ml-3 underline" href={`https://www.notion.so/${step.id.replaceAll("-", "")}`} target="_blank" rel="noreferrer">{step.name ?? "対象ページ"}</a>
        {step.kind === "properties" ? <table className="w-full text-left"><thead><tr><th>項目</th><th>変更前</th><th>確定する値</th></tr></thead><tbody>{Object.entries(step.desired as Record<string, unknown>).map(([name, value]) => <tr key={name}><th>{name}</th><td>{show((step.before as Record<string, unknown>)[name])}</td><td>{show(value)}</td></tr>)}</tbody></table> : step.kind === "relation" ? <p>{step.property}: {show(step.before)} → {show(step.desired)}</p> : null}
      </div>)}
      <p>次の実行で、残す先の値と本文・子の参照が変わり、元のページはアーカイブされます。途中で止まった場合は履歴から再開できます。</p>
      <button disabled={busy} className="rounded bg-red-700 px-4 py-2 text-white" onClick={() => run(async () => { await call({ action: "approve", operation_id: draft.id, expected_hash: draft.planHash }); setDraft(null); setComparison(null); setMessage("統合が完了しました。"); })}>確認した1組を統合する</button>
      <button disabled={busy} className="ml-3 rounded border px-4 py-2" onClick={() => run(async () => { await call({ action: "dismiss", operation_id: draft.id, expected_hash: draft.planHash }); setDraft(null); setMessage("見送りを記録しました。"); })}>今回は見送る</button>
    </div>}
    <p role="status" aria-live="polite">{busy ? "内容を確認・処理しています…" : message}</p>
    {abandonPreview && <div className="border border-amber-500 p-4 space-y-3">
      <h2 className="font-bold">途中結果を保持して中止する前の確認</h2>
      <p>完了した変更は元に戻しません。本文の追加・値の補完・子の関連変更が一部残る場合があります。元のページは残し、統合による同期停止を解除します。読取り不能と表示された項目は実物を確認してください。</p>
      <table className="w-full text-left"><thead><tr><th>手順・対象</th><th>実行記録</th><th>現在の値（中止後も保持）</th></tr></thead><tbody>{abandonPreview.snapshot.actual.map((item, index) => <tr key={index} className="border-t"><th>{stepName(item.step.kind)} <a className="underline" href={`https://www.notion.so/${item.step.id.replaceAll("-", "")}`} target="_blank" rel="noreferrer">{item.step.name ?? "対象ページ"}</a></th><td>{abandonPreview.snapshot.progress[String(index)]?.state === "done" ? "完了" : abandonPreview.snapshot.progress[String(index)]?.state === "reserved" ? "結果確認が必要" : "未実行"}</td><td>{item.readable ? show(item.current) : "読取り不能・実物を確認"}</td></tr>)}</tbody></table>
      <details><summary>全項目の詳細</summary><pre className="overflow-auto text-xs">{JSON.stringify(abandonPreview.snapshot, null, 2)}</pre></details>
      <button disabled={busy} className="border px-3 py-2" onClick={() => run(async () => { await call({action: "abandon", operation_id: abandonPreview.snapshot.id, expected_hash: abandonPreview.hash}); setAbandonPreview(null); setMessage("途中結果を保持して中止しました。必要な修正後に改めて比較できます。"); })}>途中結果を保持して中止し、同期停止を解除する</button>
    </div>}
    <h2 className="font-bold">統合の履歴・再開（1ページ50件）</h2>
    <ul className="space-y-3">{jobs.map(job => <li key={job.id} className="rounded border p-3">
      <strong>{states[job.state] ?? job.state}</strong> — <a href={`https://www.notion.so/${job.sourceId.replaceAll("-", "")}`} target="_blank" rel="noreferrer">元のページ</a> → <a href={`https://www.notion.so/${job.targetId.replaceAll("-", "")}`} target="_blank" rel="noreferrer">残す先</a>
      {job.error && <p>{job.error}</p>}
      <p>{job.updatedAt ? new Date(job.updatedAt).toLocaleString("ja-JP") : ""} — 判断者: {job.actorName ?? "登録者不明"} — 完了 {Object.values(job.progress ?? {}).filter(step => step.state === "done").length}/{job.steps?.length ?? 0} 手順</p>
      <details><summary>最近20件の処理履歴</summary><ul>{job.history?.map((entry, index) => <li key={index}>{new Date(entry.createdAt).toLocaleString("ja-JP")} — {entry.actorName ?? "登録者不明"} — {entry.afterState.step != null ? `手順 ${entry.afterState.step + 1}: ` : ""}{states[entry.afterState.state ?? ""] ?? (entry.afterState.state === "reserved" ? "書込み前の記録" : entry.afterState.state === "done" ? "完了" : "記録")}</li>)}</ul></details>
      {["approved", "running", "held"].includes(job.state) && <button disabled={busy} className="ml-3 rounded border px-3 py-1" onClick={() => run(async () => { await call({ action: "resume", operation_id: job.id }); setMessage("再開した処理の結果を履歴に反映しました。"); })}>現在値を確認して再開</button>}
      {["approved", "running", "held"].includes(job.state) && <button disabled={busy} className="ml-3 border px-3 py-1" onClick={() => run(async () => { setAbandonPreview(await call({action: "preview_abandon", operation_id: job.id})); })}>途中結果を確認して中止へ</button>}
      {job.state === "draft" && <button disabled={busy} className="ml-3 rounded border px-3 py-1" onClick={() => run(async () => { await call({ action: "dismiss", operation_id: job.id, expected_hash: job.planHash }); })}>この準備を見送る</button>}
    </li>)}</ul>
    <nav className="flex gap-4"><button disabled={busy || page === 0} onClick={() => setPage(page - 1)}>前へ</button><span>{page + 1}ページ</span><button disabled={busy || !more} onClick={() => setPage(page + 1)}>次へ</button></nav>
  </div>;
}
