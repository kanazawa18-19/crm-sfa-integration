"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import ErrorMessage from "@/components/ErrorMessage";
import { ClientSearchResult } from "@/lib/backend";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

const SEARCH_DEBOUNCE_MS = 300;

// documents/DocumentsPageClient.tsxの案件検索（デバウンス・AbortController・
// セッション切れハンドリング・レース対策のlatestQueryRef）と同じパターンを取引先検索に
// 適用したもの。360ビュー本体はここでは組み立てず、候補選択時に/clients/[id]へ遷移する
// だけの薄い検索入口とする。
export default function ClientsPageClient() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<ClientSearchResult[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);

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
          if (isSessionExpiredResponse(response)) {
            throw new Error(SESSION_EXPIRED_MESSAGE);
          }
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail ?? "取引先検索に失敗しました");
          }
          return response.json() as Promise<{
            clients: ClientSearchResult[];
            truncated: boolean;
          }>;
        })
        .then((data) => {
          if (latestQueryRef.current !== requestQuery) {
            return;
          }
          setCandidates(data.clients);
          setTruncated(data.truncated);
          setSearchError(null);
          setHasSearched(true);
        })
        .catch((error) => {
          if (error instanceof DOMException && error.name === "AbortError") {
            return;
          }
          if (latestQueryRef.current !== requestQuery) {
            return;
          }
          setCandidates([]);
          setSearchError(error instanceof Error ? error.message : "取引先検索に失敗しました");
          setHasSearched(true);
        })
        .finally(() => {
          if (latestQueryRef.current === requestQuery) {
            setSearching(false);
          }
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [query]);

  const showNoCandidates =
    query.trim() !== "" && hasSearched && !searching && !searchError && candidates.length === 0;

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="page-title">取引先(360度ビュー)</h1>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          取引先を検索すると、配下の案件・連絡先・アクション履歴・メール履歴・変更履歴を1画面にまとめて確認できます。
        </p>
      </div>

      <section>
        <div className="relative max-w-md">
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="取引先名を入力してください"
            className="input w-full"
          />
          {query.trim() !== "" && searching && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-(--color-foreground)/40">
              検索中...
            </span>
          )}
        </div>

        {query.trim() !== "" && searchError && <ErrorMessage message={searchError} />}

        {showNoCandidates && (
          <p className="mt-2 text-sm text-(--color-foreground)/60">
            該当する取引先が見つかりませんでした。
          </p>
        )}

        {query.trim() !== "" && candidates.length > 0 && (
          <div className="mt-2 max-w-md">
            <ul className="surface-card divide-y divide-(--border-subtle)">
              {candidates.map((c) => (
                <li key={c.notion_page_id}>
                  <button
                    type="button"
                    onClick={() => router.push(`/clients/${encodeURIComponent(c.notion_page_id)}`)}
                    className="w-full px-4 py-2 text-left text-sm text-(--color-foreground) hover:bg-(--color-surface-muted)"
                  >
                    {c.取引先名}
                  </button>
                </li>
              ))}
            </ul>
            {truncated && (
              <p className="mt-1 text-xs text-(--color-foreground)/60">
                さらに該当する取引先がある可能性があります。取引先名をさらに絞り込んでください。
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
