"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import ErrorMessage from "@/components/ErrorMessage";
import { ClientSearchResult, ContactSearchClientRef, ContactSearchResult } from "@/lib/backend";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

const SEARCH_DEBOUNCE_MS = 300;

// `app/(dashboard)/clients/[id]/page.tsx`のnotionPageUrl()と同じ実装（共通化は
// このファイル単体のための抽象化になるため見送り、既存の重複パターンに合わせる）。
function notionPageUrl(pageId: string): string {
  return `https://www.notion.so/${pageId.replace(/-/g, "")}`;
}

// 連絡先の`取引先`relationのボタン表示ラベル。`notion_page_id`は常に分かっているため
// 取引先名が無くてもボタン自体は出す（ChatGPTレビューWARN対応、2026-09-24。以前は
// 取引先名が無いrelationをNotionページへのリンクのみにしていたが、それでは
// 「連絡先名から取引先360ビューへ飛ぶ」という本来の目的が2件目以降で崩れていた。
// 360ビュー側が404を返すだけで実害は無い）。
// `resolved`＋名前が空（Notion側で取引先名が未入力）は「取得失敗」とは別に
// 「名称未設定」と表示する（`not_fetched`/`failed`と誤って同じ表示にしない）。
function contactClientLabel(ref: ContactSearchClientRef): string {
  if (ref.取引先名 !== null) {
    return ref.取引先名;
  }
  if (ref.取引先名_status === "resolved") {
    return "取引先（名称未設定）";
  }
  return "取引先（名称未取得）";
}

interface UseDebouncedSearchResult<T> {
  query: string;
  setQuery: (value: string) => void;
  candidates: T[];
  truncated: boolean;
  searching: boolean;
  searchError: string | null;
  hasSearched: boolean;
}

// 取引先名検索・連絡先名検索は、デバウンス・AbortController・セッション切れハンドリング・
// レース対策(latestQueryRef)が完全に同一のパターンだったため1つのフックへ切り出した
// （obasan-qualityレビューWARN対応、2026-09-24。documents/DocumentsPageClient.tsxの
// 案件検索と同じ元パターンを、取引先・連絡先の2箇所へコピペしたことで約60行が重複していた）。
// レスポンスJSONのトップレベルキー名がエンドポイントごとに異なる（`clients`/`contacts`）ため
// `itemsKey`で受け取る。挙動は切り出し前と変えない。
function useDebouncedSearch<T>(options: {
  endpoint: string;
  errorMessage: string;
  itemsKey: string;
}): UseDebouncedSearchResult<T> {
  const { endpoint, errorMessage, itemsKey } = options;
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<T[]>([]);
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

      fetch(`${endpoint}?q=${encodeURIComponent(query)}`, {
        signal: controller.signal,
        redirect: "manual",
      })
        .then(async (response) => {
          if (isSessionExpiredResponse(response)) {
            throw new Error(SESSION_EXPIRED_MESSAGE);
          }
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail ?? errorMessage);
          }
          return response.json() as Promise<Record<string, unknown>>;
        })
        .then((data) => {
          if (latestQueryRef.current !== requestQuery) {
            return;
          }
          setCandidates((data[itemsKey] as T[] | undefined) ?? []);
          setTruncated(Boolean(data.truncated));
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
          setSearchError(error instanceof Error ? error.message : errorMessage);
          setHasSearched(true);
        })
        .finally(() => {
          if (latestQueryRef.current === requestQuery) {
            setSearching(false);
          }
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [query, endpoint, errorMessage, itemsKey]);

  return { query, setQuery, candidates, truncated, searching, searchError, hasSearched };
}

// 360ビュー本体はここでは組み立てず、候補選択時に/clients/[id]へ遷移するだけの
// 薄い検索入口とする。取引先名検索・連絡先名検索は状態を分離した2つの入力欄として
// 並べる（片方の検索結果が他方の入力で消えると混乱するため）。
export default function ClientsPageClient() {
  const router = useRouter();
  const {
    query,
    setQuery,
    candidates,
    truncated,
    searching,
    searchError,
    hasSearched,
  } = useDebouncedSearch<ClientSearchResult>({
    endpoint: "/api/clients/search",
    errorMessage: "取引先検索に失敗しました",
    itemsKey: "clients",
  });

  // 連絡先名検索（2026-09-24追加）。会社名の表記ゆれで取引先名検索が0件になる相手を、
  // メールのやり取りが多い連絡先の名前から辿れるようにする。
  const {
    query: contactQuery,
    setQuery: setContactQuery,
    candidates: contactCandidates,
    truncated: contactTruncated,
    searching: contactSearching,
    searchError: contactSearchError,
    hasSearched: contactHasSearched,
  } = useDebouncedSearch<ContactSearchResult>({
    endpoint: "/api/contacts/search",
    errorMessage: "連絡先検索に失敗しました",
    itemsKey: "contacts",
  });

  const showNoCandidates =
    query.trim() !== "" && hasSearched && !searching && !searchError && candidates.length === 0;
  const showNoContactCandidates =
    contactQuery.trim() !== "" &&
    contactHasSearched &&
    !contactSearching &&
    !contactSearchError &&
    contactCandidates.length === 0;

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="page-title">取引先(360度ビュー)</h1>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          取引先を検索すると、配下の案件・連絡先・アクション履歴・メール履歴・変更履歴を1画面にまとめて確認できます。
        </p>
      </div>

      <section>
        <h2 className="text-sm font-semibold text-(--color-foreground)/80">取引先名で探す</h2>
        <div className="relative mt-2 max-w-md">
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

      <section>
        <h2 className="text-sm font-semibold text-(--color-foreground)/80">連絡先名で探す</h2>
        <p className="mt-1 text-xs text-(--color-foreground)/60">
          会社名の表記ゆれで取引先が見つからないときは、メールのやり取りがある連絡先の名前から辿れます。
        </p>
        <div className="relative mt-2 max-w-md">
          <input
            type="text"
            value={contactQuery}
            onChange={(event) => setContactQuery(event.target.value)}
            placeholder="連絡先名を入力してください"
            className="input w-full"
          />
          {contactQuery.trim() !== "" && contactSearching && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-(--color-foreground)/40">
              検索中...
            </span>
          )}
        </div>

        {contactQuery.trim() !== "" && contactSearchError && (
          <ErrorMessage message={contactSearchError} />
        )}

        {showNoContactCandidates && (
          <p className="mt-2 text-sm text-(--color-foreground)/60">
            該当する連絡先が見つかりませんでした。
          </p>
        )}

        {contactQuery.trim() !== "" && contactCandidates.length > 0 && (
          <div className="mt-2 max-w-md">
            <ul className="surface-card divide-y divide-(--border-subtle)">
              {contactCandidates.map((c) => (
                <li key={c.notion_page_id} className="px-4 py-2">
                  <p className="text-sm text-(--color-foreground)">
                    {c.名前}
                    {(c.部署 || c.役職) && (
                      <span className="ml-2 text-xs text-(--color-foreground)/50">
                        {[c.部署, c.役職].filter(Boolean).join(" / ")}
                      </span>
                    )}
                  </p>
                  {c.取引先.length === 0 ? (
                    <p className="mt-1 text-xs text-(--color-foreground)/60">
                      取引先未設定・
                      <a
                        href={notionPageUrl(c.notion_page_id)}
                        target="_blank"
                        rel="noreferrer"
                        className="link"
                      >
                        連絡先のNotionページを開く
                      </a>
                    </p>
                  ) : (
                    <ul className="mt-1 flex flex-col gap-1">
                      {c.取引先.map((ref) => (
                        <li key={ref.notion_page_id}>
                          <button
                            type="button"
                            onClick={() =>
                              router.push(`/clients/${encodeURIComponent(ref.notion_page_id)}`)
                            }
                            className="text-xs text-(--color-foreground)/80 hover:underline"
                          >
                            → {contactClientLabel(ref)}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
            {contactTruncated && (
              <p className="mt-1 text-xs text-(--color-foreground)/60">
                さらに該当する連絡先がある可能性があります。連絡先名をさらに絞り込んでください。
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
