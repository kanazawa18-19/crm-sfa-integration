"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import ErrorMessage from "@/components/ErrorMessage";
import {
  MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW,
  type BulkEmailPreview,
  type ClientSearchResult,
} from "@/lib/backend";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

// 取引先検索のデバウンス・AbortController・レース対策(latestQueryRef)は
// clients/ClientsPageClient.tsxと同じパターン。
const SEARCH_DEBOUNCE_MS = 300;

type SelectedClient = { id: string; name: string };

export default function BulkEmailPageClient({ senderName }: { senderName: string }) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<ClientSearchResult[]>([]);
  // 検索結果が上限で切られたか。出さないと「全部出ている」と誤解したまま宛先を確定してしまう。
  const [truncated, setTruncated] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selected, setSelected] = useState<SelectedClient[]>([]);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");

  const [preview, setPreview] = useState<BulkEmailPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [openedIndex, setOpenedIndex] = useState(0);

  const abortControllerRef = useRef<AbortController | null>(null);
  const latestQueryRef = useRef("");

  useEffect(() => {
    latestQueryRef.current = query;
    if (query.trim() === "") {
      abortControllerRef.current?.abort();
      // ここでsetCandidates([])しない（effect内の同期setStateは連鎖レンダーになる）。
      // 検索欄が空のときは候補を描画しない、という条件で見せ方だけ変える。
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

  function addClient(client: ClientSearchResult) {
    setSelected((current) =>
      current.some((c) => c.id === client.notion_page_id)
        ? current
        : [...current, { id: client.notion_page_id, name: client.取引先名 }]
    );
    setQuery("");
    setCandidates([]);
    setHasSearched(false);
    setTruncated(false);
  }

  function removeClient(id: string) {
    setSelected((current) => current.filter((c) => c.id !== id));
  }

  async function runPreview() {
    setPreviewing(true);
    setPreviewError(null);
    try {
      const response = await fetch("/api/bulk-email/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        redirect: "manual",
        body: JSON.stringify({
          subject,
          body,
          client_page_ids: selected.map((c) => c.id),
        }),
      });
      if (isSessionExpiredResponse(response)) throw new Error(SESSION_EXPIRED_MESSAGE);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail ?? "プレビューの作成に失敗しました");
      setPreview(payload as BulkEmailPreview);
      setOpenedIndex(0);
    } catch (error) {
      setPreview(null);
      setPreviewError(error instanceof Error ? error.message : String(error));
    } finally {
      setPreviewing(false);
    }
  }

  const opened = preview?.messages[openedIndex] ?? null;
  // 押してから422で知る、をやめる（バックエンド側でも同じ上限で弾いている）。
  const overLimit = selected.length > MAX_CLIENTS_PER_BULK_EMAIL_PREVIEW;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="page-title">一斉配信</h1>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          差出人: {senderName || "（表示名が未設定です。プロフィール編集で登録してください）"}
        </p>
      </div>

      {/* この画面が「送らない」ことは、注意書きではなく最初に読む位置に置く。
          送れると思って操作した人が、送信ボタンを探して迷うのを防ぐため。 */}
      <div className="alert-warning">
        <strong>この画面はまだ送信できません。</strong>
        宛先と、差し込み後の本文を確認するためのものです。実際に送るには送信経路
        （営業担当のGmailから送るかどうか）の決定が必要です。
      </div>

      {/* 「宛先が0件」の理由の大半は根拠の未登録になる。押す前に行き先が見えているようにする。 */}
      <p className="text-sm text-(--color-foreground)/60">
        宛先になるのは、送ってよい根拠が登録されている連絡先だけです。
        <Link href="/bulk-email/consent" className="ml-2 underline">
          送信根拠の管理へ
        </Link>
      </p>

      <section className="surface-card border-(--border-subtle) p-5">
        <h2 className="text-base font-bold">1. 宛先を選ぶ</h2>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          取引先を選ぶと、その取引先にぶら下がっている連絡先が宛先になります。
        </p>
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
                  aria-label={`${client.name}を宛先から外す`}
                  className="text-(--color-foreground)/50 hover:text-(--brand-danger)"
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
      </section>

      <section className="surface-card border-(--border-subtle) p-5">
        <h2 className="text-base font-bold">2. 文面を作る</h2>
        <label className="mt-3 block text-sm font-semibold" htmlFor="bulk-subject">
          件名
        </label>
        <input
          id="bulk-subject"
          className="input mt-1 w-full"
          value={subject}
          onChange={(event) => setSubject(event.target.value)}
        />
        <label className="mt-4 block text-sm font-semibold" htmlFor="bulk-body">
          本文
        </label>
        <textarea
          id="bulk-body"
          className="input mt-1 h-64 w-full font-mono text-sm"
          value={body}
          onChange={(event) => setBody(event.target.value)}
          placeholder={"{{会社名}}\n{{氏名}} 様\n\nいつもお世話になっております。"}
        />
        <p className="mt-2 text-xs text-(--color-foreground)/60">
          差し込みは <code>{"{{会社名}}"}</code> <code>{"{{氏名}}"}</code>{" "}
          <code>{"{{部署}}"}</code> <code>{"{{役職}}"}</code> <code>{"{{担当者名}}"}</code>{" "}
          が使えます。会社名・住所・配信停止リンクは送信時に自動で末尾へ付くため、本文には書きません。
        </p>
      </section>

      <div>
        <button
          type="button"
          className="btn-primary"
          onClick={runPreview}
          disabled={previewing || selected.length === 0 || overLimit}
        >
          {previewing ? "作成中..." : "プレビューを作る"}
        </button>
        {selected.length === 0 && (
          <span className="ml-3 text-sm text-(--color-foreground)/50">
            取引先を1社以上選んでください
          </span>
        )}
      </div>

      {previewError && <ErrorMessage message={previewError} />}

      {preview && (
        <section className="flex flex-col gap-4">
          {preview.blockers.map((blocker) => (
            <p key={blocker} className="alert-error">
              {blocker}
              {blocker.includes("送ってよい根拠") && (
                <Link href="/bulk-email/consent" className="ml-2 underline">
                  送信根拠の管理へ
                </Link>
              )}
            </p>
          ))}
          {preview.warnings.map((warning) => (
            <p key={warning} className="alert-warning">
              {warning}
            </p>
          ))}

          <p className="text-sm">
            送れる宛先 <strong>{preview.counts.sendable}</strong> 件 / 外した宛先{" "}
            <strong>{preview.counts.skipped}</strong> 件
          </p>
          {preview.sendable && (
            <p className="alert-success">
              宛先と法定表示の確認は通りました。
              <strong>ただし送信機能はまだ無いため、この内容は送られていません。</strong>
            </p>
          )}

          {preview.messages.length > 0 && (
            <div className="surface-card border-(--border-subtle) overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>取引先</th>
                    <th>氏名</th>
                    <th>メールアドレス</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {preview.messages.map((message, index) => (
                    <tr
                      key={message.contact_page_id}
                      // 件数が多いと「今どれの本文を見ているか」を見失うため、開いている行を残す。
                      className={index === openedIndex ? "bg-(--color-surface-muted)" : undefined}
                    >
                      <td>{message.client_name}</td>
                      <td>{message.contact_name}</td>
                      <td>{message.to_email}</td>
                      <td>
                        <button
                          type="button"
                          className="btn-ghost btn-xs"
                          onClick={() => setOpenedIndex(index)}
                        >
                          本文を見る
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {opened && (
            <div className="surface-card border-(--border-subtle) p-5">
              <p className="text-sm text-(--color-foreground)/60">
                {opened.client_name} / {opened.contact_name}（{opened.to_email}）宛
              </p>
              <p className="mt-2 text-sm font-bold">件名: {opened.subject}</p>
              <pre className="mt-3 max-h-96 overflow-auto rounded-[6px] bg-(--color-surface-muted) p-4 text-sm whitespace-pre-wrap">
                {opened.body}
              </pre>
            </div>
          )}

          {preview.skipped.length > 0 && (
            <div className="surface-card border-(--border-subtle) overflow-x-auto">
              <p className="px-5 pt-4 text-sm font-bold">外した宛先</p>
              <p className="px-5 pb-2 text-xs text-(--color-foreground)/60">
                減った宛先は全部ここに出ます。黙って落とすことはしません。
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>取引先</th>
                    <th>氏名</th>
                    <th>メールアドレス</th>
                    <th>外した理由</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.skipped.map((item) => (
                    <tr key={`${item.contact_page_id}-${item.reason}`}>
                      <td>{item.client_name}</td>
                      <td>{item.contact_name}</td>
                      <td>{item.email}</td>
                      <td>
                        {item.reason_label}
                        {item.detail && (
                          <span className="text-(--color-foreground)/50">（{item.detail}）</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
