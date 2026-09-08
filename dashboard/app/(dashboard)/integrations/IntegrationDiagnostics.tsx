"use client";
import { useEffect, useRef, useState } from "react";
import { INTEGRATION_TARGETS, STATUS_LABELS, SUCCESS_LABELS, type DiagnosticResult, type DiagnosticStatus, type IntegrationTarget } from "@/lib/integrationDiagnostics";
const COLORS: Record<DiagnosticStatus, string> = { ok: "bg-green-100 text-green-900", failed: "bg-red-100 text-red-900", not_configured: "bg-amber-100 text-amber-900", unknown: "bg-gray-100 text-gray-700" };

export default function IntegrationDiagnostics() {
  const [results, setResults] = useState<Partial<Record<IntegrationTarget, DiagnosticResult>>>({});
  const [active, setActive] = useState<IntegrationTarget | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState("");
  const busy = useRef(false);
  const runController = useRef<AbortController | null>(null);
  useEffect(() => () => {
    // 画面離脱時は後続診断を止める。送信済みのサーバー処理の停止は保証しない。
    runController.current?.abort();
  }, []);
  async function run(targets: readonly IntegrationTarget[]) {
    if (busy.current) return;
    busy.current = true;
    const controller = new AbortController();
    runController.current = controller;
    setRunning(true);
    try {
      for (const [index, target] of targets.entries()) {
        if (controller.signal.aborted) return;
        setActive(target);
        setProgress(`${index + 1} / ${targets.length} 件目を確認中`);
        const start = performance.now();
        let result: DiagnosticResult;
        let accessDenied = false;
        try {
          const response = await fetch("/api/diagnostics/integrations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }), signal: AbortSignal.any([controller.signal, AbortSignal.timeout(55_000)]), cache: "no-store", redirect: "error" });
          if (response.status === 401 || response.status === 403) {
            accessDenied = true;
            result = { target, status: "unknown", message: response.status === 401 ? "ログインの有効期限が切れました。再ログインしてから確認してください。" : "管理者権限または送信元を確認できませんでした。管理担当者に確認してください。", checkedAt: new Date().toISOString(), elapsedMs: Math.round(performance.now() - start), facts: [] };
          } else {
            if (!response.ok) throw new Error("診断取得失敗");
            result = await response.json() as DiagnosticResult;
          }
        } catch {
          result = { target, status: "unknown", message: "確認できませんでした。ログイン状態や通信を確認して再実行してください。", checkedAt: new Date().toISOString(), elapsedMs: Math.round(performance.now() - start), facts: [] };
        }
        if (controller.signal.aborted) return;
        setResults((previous) => ({ ...previous, [target]: result }));
        if (accessDenied) {
          setProgress("ログイン・権限の確認が必要なため、後続の診断を停止しました。");
          return;
        }
      }
      setProgress(`${targets.length}件の確認を終了しました。結果を確認してください。`);
    } finally {
      if (!controller.signal.aborted) { setActive(null); setRunning(false); }
      busy.current = false;
      if (runController.current === controller) runController.current = null;
    }
  }
  const counts = { ok: 0, failed: 0, not_configured: 0, unknown: 0 };
  for (const target of INTEGRATION_TARGETS) counts[results[target.id]?.status ?? "unknown"]++;
  return <div className="mx-auto max-w-5xl space-y-6">
    <div><h1 className="text-2xl font-bold">連携状態</h1><p className="mt-2 text-sm text-(--color-foreground)/70">外部サービスの接続・設定と、変更通知の受信・行作成待ちを手動で確認します。</p></div>
    <div className="space-y-2 rounded-lg border border-(--border-subtle) surface-card p-4 text-sm">
      <p>画面を開くだけでは診断しません。「すべて確認」または各項目の「確認」で開始します。</p>
      <p>正常は各項目の診断範囲内の結果です。同期完了やSlack通知の到達を保証しません。データの同期・修復や通知送信は行いません。</p>
      <p>結果はこの画面を開いている間だけ保持します。確認時刻は日本時間です。すべての確認には数分かかる場合があります。</p>
    </div>
    <div className="flex flex-wrap items-center gap-3"><button type="button" disabled={running} onClick={() => void run(INTEGRATION_TARGETS.map((target) => target.id))} className="rounded-lg bg-(--color-foreground) px-4 py-2 text-sm font-medium text-(--color-background) disabled:opacity-50">{running ? "確認中…" : "すべて確認"}</button><p role="status" aria-live="polite" className="text-sm">{progress || "まだ診断していません"}</p></div>
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">{(Object.keys(counts) as DiagnosticStatus[]).map((status) => <div key={status} className="rounded-lg border border-(--border-subtle) p-3"><span className="text-xs">{STATUS_LABELS[status]}</span><p className="mt-1 text-xl font-semibold">{counts[status]}件</p></div>)}</div>
    <div className="space-y-3">{INTEGRATION_TARGETS.map((target) => {
      const result = results[target.id];
      const status = result?.status ?? "unknown";
      const checking = active === target.id;
      return <section key={target.id} aria-labelledby={`title-${target.id}`} aria-busy={checking} className="rounded-lg border border-(--border-subtle) surface-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 id={`title-${target.id}`} className="font-semibold">{target.label}</h2><p className="mt-1 text-sm text-(--color-foreground)/70">{target.scope}</p></div><div className="flex items-center gap-3"><span className={`rounded px-2 py-1 text-xs font-medium ${COLORS[status]}`}>{checking ? "確認中" : status === "ok" ? SUCCESS_LABELS[target.id] : STATUS_LABELS[status]}</span><button type="button" disabled={running} aria-label={`${target.label}を${result ? "再確認" : "確認"}`} onClick={() => void run([target.id])} className="rounded border border-(--border-subtle) px-3 py-2 text-sm disabled:opacity-50">{result ? "再確認" : "確認"}</button></div></div>
        {result ? <div className="mt-3 space-y-2 text-sm"><p>{checking ? "以下は前回の結果です。" : ""}{result.message}</p><p className="text-xs text-(--color-foreground)/60">確認時刻：{new Date(result.checkedAt).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" })} ／ 所要時間：{(result.elapsedMs / 1000).toFixed(1)}秒</p>{result.facts.length > 0 && <dl className="grid gap-2 sm:grid-cols-2">{result.facts.map((fact) => <div key={fact.label} className="rounded bg-(--color-surface-muted) p-2"><dt className="text-xs text-(--color-foreground)/70">{fact.label}</dt><dd className="break-words">{fact.value}</dd></div>)}</dl>}{target.id === "webhook_receipts" && <p className="text-xs">受信がない場合、変更がなかった可能性もあります。受信なしだけでは異常と判定しません。</p>}</div> : <p className="mt-3 text-sm text-(--color-foreground)/60">{checking ? "診断結果を待っています。" : "未実行です。確認ボタンを押してください。"}</p>}
      </section>;
    })}</div>
  </div>;
}
