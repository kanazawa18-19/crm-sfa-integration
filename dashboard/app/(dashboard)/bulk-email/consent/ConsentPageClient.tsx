"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import ErrorMessage from "@/components/ErrorMessage";
import {
  MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW,
  type BulkEmailConsentContact,
  type BulkEmailConsentOverview,
  type ClientSearchResult,
} from "@/lib/backend";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

// 取引先検索のデバウンス・AbortController・レース対策は BulkEmailPageClient.tsx と同じパターン。
const SEARCH_DEBOUNCE_MS = 300;

type SelectedClient = { id: string; name: string };

type FormState = {
  contactPageId: string;
  basis: string;
  obtainedAt: string;
  evidence: string;
};

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/** 今の状態を1行で表す。色は「送れるか」だけで決める(理由の細かさで色を増やさない)。 */
function ConsentBadge({ contact }: { contact: BulkEmailConsentContact }) {
  if (contact.unsubscribed) {
    return <span className="badge-gold">配信停止の申し出あり</span>;
  }
  if (!contact.consent.allowed) {
    return <span className="badge-muted">{contact.consent.reason_label}</span>;
  }
  return (
    <span className="badge-green">
      {contact.consent.basis_label}
      {contact.consent.stale && `（${contact.consent.stale_label}）`}
    </span>
  );
}

export default function ConsentPageClient() {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<ClientSearchResult[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selected, setSelected] = useState<SelectedClient[]>([]);
  const [overview, setOverview] = useState<BulkEmailConsentOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [form, setForm] = useState<FormState | null>(null);
  // 直前に登録した内容。同じ展示会の名刺を何人ぶんも入れるとき、毎回打ち直させない
  // (1件ずつ人が判断する原則は変えず、打鍵だけ減らす。obasan-qualityレビュー指摘)。
  const [lastEntry, setLastEntry] = useState<Omit<FormState, "contactPageId"> | null>(null);
  // 取り消し済みの根拠を復活させようとしたときだけ立つ。ボタンの文言を変えて、
  // 「訂正である」と分かった上で押してもらう。
  const [needsReactivate, setNeedsReactivate] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const latestQueryRef = useRef("");

  useEffect(() => {
    latestQueryRef.current = query;
    if (query.trim() === "") {
      abortControllerRef.current?.abort();
      return;
    }

    const timer = setTimeout(() => {
      abortControllerRef.current?.abort();
      const controller = new AbortController();
      abortControllerRef.current = controller;
      const requestQuery = query;
      setSearching(true);

      fetch(`/api/clients/search?q=${encodeURIComponent(query)}`, {
        signal: controller.signal,
        redirect: "manual",
      })
        .then(async (response) => {
          if (isSessionExpiredResponse(response)) throw new Error(SESSION_EXPIRED_MESSAGE);
          if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.detail ?? "取引先検索に失敗しました");
          }
          return response.json() as Promise<{
            clients: ClientSearchResult[];
            truncated: boolean;
          }>;
        })
        .then((data) => {
          if (latestQueryRef.current !== requestQuery) return;
          setCandidates(data.clients);
          setTruncated(data.truncated);
          setHasSearched(true);
          setSearchError(null);
        })
        .catch((error: Error) => {
          if (error.name === "AbortError") return;
          setSearchError(error.message);
        })
        .finally(() => {
          if (latestQueryRef.current === requestQuery) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [query]);

  const loadOverview = useCallback(async (clientIds: string[]) => {
    if (clientIds.length === 0) {
      setOverview(null);
      return;
    }
    if (clientIds.length > MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW) {
      // バックエンドも422で弾くが、押してから知るのをやめる。
      setOverview(null);
      setLoadError(
        `一度に選べる取引先は${MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW}社までです。減らしてください。`
      );
      return;
    }
    setLoading(true);
    setLoadError(null);
    try {
      const response = await fetch("/api/bulk-email/consent-overview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        redirect: "manual",
        body: JSON.stringify({ client_page_ids: clientIds }),
      });
      if (isSessionExpiredResponse(response)) throw new Error(SESSION_EXPIRED_MESSAGE);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail ?? "連絡先の取得に失敗しました");
      setOverview(payload as BulkEmailConsentOverview);
    } catch (error) {
      setOverview(null);
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, []);

  // 取引先を選び直したら、その場で読み直す。**effect経由にしない** —
  // 選択の変更で連鎖レンダーが起きる上に、「いつ読み直されたか」が追いにくくなる。
  // 古い一覧を見たまま登録するのが一番まずいので、変更した本人の操作から必ず読み直す。
  function selectClients(next: SelectedClient[]) {
    setSelected(next);
    setForm(null);
    void loadOverview(next.map((client) => client.id));
  }

  function addClient(client: ClientSearchResult) {
    if (!selected.some((c) => c.id === client.notion_page_id)) {
      selectClients([...selected, { id: client.notion_page_id, name: client.取引先名 }]);
    }
    setQuery("");
    setCandidates([]);
    setHasSearched(false);
    setTruncated(false);
  }

  function removeClient(id: string) {
    selectClients(selected.filter((c) => c.id !== id));
  }

  function openForm(contact: BulkEmailConsentContact) {
    setSaveError(null);
    setNotice(null);
    // 既に登録がある連絡先は今の内容を、無い連絡先は直前に入れた内容を初期値にする。
    setForm({
      contactPageId: contact.contact_page_id,
      basis:
        contact.consent.basis ||
        lastEntry?.basis ||
        (overview?.basis_options[0]?.value ?? ""),
      obtainedAt: contact.consent.obtained_at || lastEntry?.obtainedAt || todayIso(),
      evidence: contact.consent.evidence || lastEntry?.evidence || "",
    });
  }

  async function submitForm(contact: BulkEmailConsentContact, reactivate = false) {
    if (!form) return;
    // 取引先はバックエンドが連絡先ごとに返したIDを使う。取引先名で逆引きすると、
    // 同名の取引先を同時に選んだときに取り違える(3体が独立に指摘、2026-09-03)。
    const clientPageId = contact.client_page_id;
    if (!clientPageId) {
      setSaveError("この連絡先の取引先が分かりませんでした。取引先を選び直してください。");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const response = await fetch("/api/bulk-email/consent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        redirect: "manual",
        body: JSON.stringify({
          client_page_id: clientPageId,
          contact_page_id: form.contactPageId,
          basis: form.basis,
          obtained_at: form.obtainedAt,
          evidence: form.evidence,
          // 取り消し済みの根拠を復活させるのは訂正であって、通常の登録とは別の判断。
          // 画面で「取り消しを解除して登録」を選んだときだけ true を送る。
          reactivate,
        }),
      });
      if (isSessionExpiredResponse(response)) throw new Error(SESSION_EXPIRED_MESSAGE);
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409) {
        // 取り消し済み。何が起きているかを見せて、明示的に選び直してもらう。
        setNeedsReactivate(true);
        setSaveError(payload.detail ?? "この連絡先の根拠は取り消されています。");
        return;
      }
      if (!response.ok) throw new Error(payload.detail ?? "根拠の登録に失敗しました");
      setLastEntry({
        basis: form.basis,
        obtainedAt: form.obtainedAt,
        evidence: form.evidence,
      });
      setForm(null);
      setNotice(
        `${contact.contact_name} さんの根拠を登録しました。` +
          "同じ内容は次の連絡先の初期値として引き継ぎます。"
      );
      await loadOverview(selected.map((client) => client.id));
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  async function revoke(contact: BulkEmailConsentContact) {
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      const response = await fetch("/api/bulk-email/consent", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        redirect: "manual",
        body: JSON.stringify({
          // 登録と同じ所属確認を通すため、取引先IDも一緒に送る。
          client_page_id: contact.client_page_id,
          contact_page_id: contact.contact_page_id,
        }),
      });
      if (isSessionExpiredResponse(response)) throw new Error(SESSION_EXPIRED_MESSAGE);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail ?? "根拠の取り消しに失敗しました");
      setNotice(`${contact.contact_name} さんの根拠を取り消しました。以後は送られません。`);
      await loadOverview(selected.map((client) => client.id));
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  const overLimit = selected.length > MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW;
  const selectedBasis = overview?.basis_options.find((option) => option.value === form?.basis);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="page-title">送信根拠の管理</h1>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          一斉配信は、ここに根拠が登録されている連絡先にしか送りません。
          <Link href="/bulk-email" className="ml-2 underline">
            一斉配信の画面へ戻る
          </Link>
        </p>
      </div>

      <div className="alert-warning">
        <strong>登録されていない連絡先には送りません。</strong>
        広告・宣伝のメールは、あらかじめ同意を得た相手や、名刺交換などでアドレスを
        教えてもらった相手に送るのが原則です（特定電子メール法）。
        「配信停止の申し出が無いこと」は、送ってよい理由になりません。
        <strong>心当たりの無い相手をここで登録しないでください。</strong>
        <br />
        根拠は<strong>登録したときのメールアドレス</strong>に紐づきます。
        連絡先のアドレスが変わったら、その根拠は効かなくなるので登録し直してください。
        同じ人が別の連絡先としても登録されている場合は、両方に登録が要ります。
      </div>

      <section className="surface-card border-(--border-subtle) p-5">
        <h2 className="text-base font-bold">1. 取引先を選ぶ</h2>
        <input
          className="input mt-3 w-full"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="取引先名で検索"
          aria-label="取引先名で検索"
        />
        {searching && <p className="mt-2 text-sm text-(--color-foreground)/50">検索中...</p>}
        {searchError && <ErrorMessage message={searchError} />}
        {query.trim() !== "" && candidates.length > 0 && (
          <ul className="mt-2 flex flex-col gap-1">
            {candidates.map((client) => (
              <li key={client.notion_page_id}>
                <button
                  type="button"
                  onClick={() => addClient(client)}
                  className="w-full rounded-[6px] px-3 py-2 text-left text-sm hover:bg-(--color-surface-muted)"
                >
                  {client.取引先名}
                </button>
              </li>
            ))}
          </ul>
        )}
        {query.trim() !== "" && !searching && hasSearched && candidates.length === 0 && (
          <p className="mt-2 text-sm text-(--color-foreground)/60">
            該当する取引先が見つかりませんでした。
          </p>
        )}
        {query.trim() !== "" && truncated && (
          <p className="mt-2 text-sm text-(--color-foreground)/60">
            他にも該当する取引先がある可能性があります。絞り込んで検索してください。
          </p>
        )}
        <p className="mt-3 text-xs text-(--color-foreground)/60">
          選択中 {selected.length} / {MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW} 社
          {overLimit && "（上限を超えています。減らしてください）"}
        </p>
        {selected.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {selected.map((client) => (
              <span key={client.id} className="badge-blue gap-2">
                {client.name || "（名称未設定）"}
                <button
                  type="button"
                  onClick={() => removeClient(client.id)}
                  aria-label={`${client.name}を外す`}
                  className="text-(--color-foreground)/50 hover:text-(--brand-danger)"
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
      </section>

      {loading && <p className="text-sm text-(--color-foreground)/50">連絡先を読み込み中...</p>}
      {loadError && <ErrorMessage message={loadError} />}
      {saveError && <ErrorMessage message={saveError} />}
      {notice && <p className="alert-success">{notice}</p>}

      {overview && (
        <section className="flex flex-col gap-4">
          {overview.warnings.map((warning) => (
            <p key={warning} className="alert-warning">
              {warning}
            </p>
          ))}

          <p className="text-sm">
            連絡先 <strong>{overview.counts.total}</strong> 件 / 送れる状態{" "}
            <strong>{overview.counts.allowed}</strong> 件 / 配信停止{" "}
            <strong>{overview.counts.unsubscribed}</strong> 件
          </p>

          <div className="surface-card border-(--border-subtle) overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>取引先</th>
                  <th>氏名</th>
                  <th>メールアドレス</th>
                  <th>今の状態</th>
                  <th>根拠の内容</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {overview.contacts.map((contact) => (
                  <tr key={contact.contact_page_id}>
                    <td>{contact.client_name}</td>
                    <td>{contact.contact_name}</td>
                    <td>{contact.email || "（未登録）"}</td>
                    <td>
                      <ConsentBadge contact={contact} />
                    </td>
                    <td className="text-xs text-(--color-foreground)/70">
                      {contact.consent.obtained_at && (
                        <div>取得日: {contact.consent.obtained_at}</div>
                      )}
                      {contact.consent.evidence && <div>{contact.consent.evidence}</div>}
                      {contact.consent.recorded_by && (
                        <div className="text-(--color-foreground)/50">
                          登録: {contact.consent.recorded_by}
                        </div>
                      )}
                      {contact.consent.detail && !contact.consent.allowed && (
                        <div className="text-(--color-foreground)/50">
                          {contact.consent.reason === "consent_email_mismatch"
                            ? `登録時のアドレス: ${contact.consent.detail}`
                            : contact.consent.detail}
                        </div>
                      )}
                    </td>
                    <td className="whitespace-nowrap">
                      <button
                        type="button"
                        className="btn-ghost btn-xs"
                        onClick={() => openForm(contact)}
                      >
                        {contact.consent.basis ? "登録し直す" : "根拠を登録"}
                      </button>
                      {contact.consent.allowed && (
                        <button
                          type="button"
                          className="btn-ghost btn-xs text-(--brand-danger)"
                          onClick={() => void revoke(contact)}
                          disabled={saving}
                        >
                          取り消す
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {form &&
            (() => {
              const target = overview.contacts.find(
                (contact) => contact.contact_page_id === form.contactPageId
              );
              if (!target) return null;
              return (
                <div className="surface-card border-(--border-subtle) p-5">
                  <h2 className="text-base font-bold">
                    {target.client_name} / {target.contact_name} さんの根拠
                  </h2>
                  {target.unsubscribed && (
                    <p className="alert-warning mt-3">
                      この方は配信停止を申し出ています。根拠を登録しても送られません。
                    </p>
                  )}

                  <label className="mt-4 block text-sm font-semibold" htmlFor="consent-basis">
                    根拠の種類
                  </label>
                  <select
                    id="consent-basis"
                    className="input mt-1 w-full"
                    value={form.basis}
                    onChange={(event) => setForm({ ...form, basis: event.target.value })}
                  >
                    {overview.basis_options.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  {selectedBasis && (
                    <p className="mt-1 text-xs text-(--color-foreground)/60">
                      {selectedBasis.description}
                    </p>
                  )}

                  <label className="mt-4 block text-sm font-semibold" htmlFor="consent-date">
                    根拠を得た日
                  </label>
                  <input
                    id="consent-date"
                    type="date"
                    className="input mt-1"
                    max={todayIso()}
                    value={form.obtainedAt}
                    onChange={(event) => setForm({ ...form, obtainedAt: event.target.value })}
                  />

                  <label className="mt-4 block text-sm font-semibold" htmlFor="consent-evidence">
                    取得元・証跡
                  </label>
                  <textarea
                    id="consent-evidence"
                    className="input mt-1 h-24 w-full text-sm"
                    value={form.evidence}
                    placeholder={selectedBasis?.evidence_hint ?? ""}
                    onChange={(event) => setForm({ ...form, evidence: event.target.value })}
                  />
                  <p className="mt-1 text-xs text-(--color-foreground)/60">
                    後から他の人が裏を取れる形で書いてください。空では登録できません。
                  </p>

                  <div className="mt-4 flex gap-2">
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={saving}
                      onClick={() => void submitForm(target, needsReactivate)}
                    >
                      {saving
                        ? "登録中..."
                        : needsReactivate
                          ? "取り消しを解除して登録する"
                          : "この内容で登録する"}
                    </button>
                    <button type="button" className="btn-ghost" onClick={() => setForm(null)}>
                      やめる
                    </button>
                  </div>
                </div>
              );
            })()}
        </section>
      )}
    </div>
  );
}
