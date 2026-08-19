"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import ErrorMessage from "@/components/ErrorMessage";
import { ProjectSearchResult } from "@/lib/backend";
import {
  buildFallbackFilename,
  DOCUMENT_CATEGORIES,
  DocumentCategory,
  parseDocumentNotes,
  parseFilenameFromContentDisposition,
  splitDocumentNotes,
} from "@/lib/documents";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

const SEARCH_DEBOUNCE_MS = 300;

export interface DocumentApproverOption {
  id: string;
  name: string;
  email: string;
  title: string | null;
}

export default function DocumentsPageClient({
  driveConnected,
  approvers,
  creatorNameDefault,
}: {
  driveConnected: boolean;
  approvers: DocumentApproverOption[];
  creatorNameDefault: string;
}) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<ProjectSearchResult[]>([]);
  const [totalMatched, setTotalMatched] = useState(0);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [selectedProject, setSelectedProject] = useState<ProjectSearchResult | null>(null);
  const [category, setCategory] = useState<DocumentCategory>(DOCUMENT_CATEGORIES[0]);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [documentNotes, setDocumentNotes] = useState<string[] | null>(null);
  const [generatedFilename, setGeneratedFilename] = useState<string | null>(null);

  // 見積書の手動入力欄(2026-08-19追加)。Notion案件データを上書きしたい場合や、Notion側に
  // 対応項目が無い商材名・初期費用・月額費用を差し込みたい場合に使う。全項目任意。
  // 作成者はログイン中ユーザーの表示名で初期値を入れる(見積書NOの採番に使われる——
  // src/document_generation/quote_generator._generate_quote_number参照)が、他の担当者が
  // 代理作成する場合等に備えて編集可能にしてある。
  const [memo, setMemo] = useState("");
  const [initialFee, setInitialFee] = useState("");
  const [monthlyFee, setMonthlyFee] = useState("");
  const [clientNameOverride, setClientNameOverride] = useState("");
  const [serviceName, setServiceName] = useState("");
  const [creatorName, setCreatorName] = useState(creatorNameDefault);

  // 見積書の承認リクエスト送信(2026-08-18)。
  // 先頭の承認者を自動選択すると誤って別の承認者へ送るリスクがあるため、初期値は
  // 「未選択」にする(obasan-qualityレビューWARN対応)。
  const [approverEmail, setApproverEmail] = useState("");
  const [approvalMessage, setApprovalMessage] = useState("");
  const [requestingApproval, setRequestingApproval] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [approvalSuccess, setApprovalSuccess] = useState(false);

  const abortControllerRef = useRef<AbortController | null>(null);
  // 案件選択直後、選択した案件名で検索effectが再発火し候補ドロップダウンが
  // 再表示されてしまう問題を避けるためのフラグ（shirokuma-secレビュー指摘を反映）。
  const skipNextSearchRef = useRef(false);
  const latestQueryRef = useRef("");

  useEffect(() => {
    latestQueryRef.current = query;

    if (skipNextSearchRef.current) {
      skipNextSearchRef.current = false;
      return;
    }

    if (query.trim() === "") {
      // effect本体での同期的setState呼び出しはcascading renderを招くため避ける
      // （react-hooks/set-state-in-effect）。空クエリ時はcandidates等のstateを
      // クリアせず、render側のガード条件（query.trim() !== ""）で非表示にする。
      abortControllerRef.current?.abort();
      return;
    }

    const timer = setTimeout(() => {
      abortControllerRef.current?.abort();
      const controller = new AbortController();
      abortControllerRef.current = controller;
      const requestQuery = query;
      setSearching(true);

      fetch(`/api/projects/search?q=${encodeURIComponent(query)}`, {
        signal: controller.signal,
        redirect: "manual",
      })
        .then(async (response) => {
          // proxy.tsが未認証アクセスを/loginへ302リダイレクトする際、redirect:"manual"
          // 指定時はopaqueredirectとして観測される（デフォルトのfollowだとログインページの
          // HTMLをそのまま正常レスポンスとして扱ってしまう。生成側の同種の問題は
          // shirokuma-secレビューで検出、検索側も念のため揃えて対処）。
          if (isSessionExpiredResponse(response)) {
            throw new Error(SESSION_EXPIRED_MESSAGE);
          }
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail ?? "案件検索に失敗しました");
          }
          return response.json() as Promise<{
            projects: ProjectSearchResult[];
            total_matched: number;
          }>;
        })
        .then((data) => {
          // デバウンス中に別クエリへの入力が進んでいた場合、古いレスポンスで
          // 新しい入力中の候補を上書きしないようにする（shirokuma-secレビュー指摘を反映）。
          if (latestQueryRef.current !== requestQuery) {
            return;
          }
          setCandidates(data.projects);
          setTotalMatched(data.total_matched);
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
          setSearchError(error instanceof Error ? error.message : "案件検索に失敗しました");
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

  function handleSelectProject(project: ProjectSearchResult) {
    skipNextSearchRef.current = true;
    setSelectedProject(project);
    setCandidates([]);
    setTotalMatched(0);
    setHasSearched(false);
    setQuery(project.project_name);
    setDocumentNotes(null);
    setGeneratedFilename(null);
    setGenerateError(null);
    setApprovalError(null);
    setApprovalSuccess(false);
  }

  async function handleGenerate() {
    if (!selectedProject) {
      return;
    }

    setGenerating(true);
    setGenerateError(null);
    setDocumentNotes(null);
    setGeneratedFilename(null);

    try {
      const params = new URLSearchParams({
        notion_project_id: selectedProject.notion_page_id,
        category,
      });
      if (category === "見積書") {
        const overrideEntries: [string, string][] = [
          ["memo", memo],
          ["initial_fee", initialFee],
          ["monthly_fee", monthlyFee],
          ["client_name", clientNameOverride],
          ["service_name", serviceName],
          ["creator_name", creatorName],
        ];
        for (const [key, value] of overrideEntries) {
          if (value.trim() !== "") {
            params.set(key, value.trim());
          }
        }
      }
      const response = await fetch(`/api/documents/generate?${params.toString()}`, {
        redirect: "manual",
      });

      // BLOCKER相当の実バグ修正: デフォルトのfetchはリダイレクトを追従するため、
      // セッション切れ時に/loginのHTMLがそのまま「見積書.pdf」等としてダウンロード
      // されてしまっていた（shirokuma-secレビューで検出）。redirect:"manual"で
      // リダイレクトをopaqueredirectとして検知し、明示的にエラー扱いにする。
      if (isSessionExpiredResponse(response)) {
        throw new Error(SESSION_EXPIRED_MESSAGE);
      }

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? "書類の生成に失敗しました");
      }

      const notes = parseDocumentNotes(response.headers.get("X-Document-Notes"));
      const filename =
        parseFilenameFromContentDisposition(response.headers.get("Content-Disposition")) ??
        buildFallbackFilename(selectedProject.project_name, category);

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      setDocumentNotes(notes);
      setGeneratedFilename(filename);
    } catch (error) {
      setGenerateError(error instanceof Error ? error.message : "書類の生成に失敗しました");
    } finally {
      setGenerating(false);
    }
  }

  async function handleRequestApproval() {
    if (!selectedProject || !approverEmail) {
      return;
    }

    setRequestingApproval(true);
    setApprovalError(null);
    setApprovalSuccess(false);

    try {
      const response = await fetch("/api/documents/quote/request-approval", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        redirect: "manual",
        body: JSON.stringify({
          project_id: selectedProject.notion_page_id,
          approver_email: approverEmail,
          message: approvalMessage,
          memo,
          initial_fee: initialFee,
          monthly_fee: monthlyFee,
          client_name: clientNameOverride,
          service_name: serviceName,
          creator_name: creatorName,
        }),
      });

      if (isSessionExpiredResponse(response)) {
        throw new Error(SESSION_EXPIRED_MESSAGE);
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? "承認リクエストの送信に失敗しました");
      }

      setApprovalSuccess(true);
    } catch (error) {
      setApprovalError(error instanceof Error ? error.message : "承認リクエストの送信に失敗しました");
    } finally {
      setRequestingApproval(false);
    }
  }

  const { baselineNote, specificNotes } =
    documentNotes !== null ? splitDocumentNotes(documentNotes) : { baselineNote: null, specificNotes: [] };
  const showNoCandidates =
    query.trim() !== "" && hasSearched && !searching && !searchError && candidates.length === 0;
  const hiddenMatchCount = totalMatched - candidates.length;

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">書類作成</h1>
        <p className="mt-1 text-sm text-gray-500">
          案件を検索し、見積書・申込書・契約書を生成します。
        </p>
      </div>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">案件検索</h2>
        <div className="relative max-w-md">
          <input
            type="text"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setSelectedProject(null);
            }}
            placeholder="案件名を入力してください"
            className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
          />
          {query.trim() !== "" && searching && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-gray-400">
              検索中...
            </span>
          )}
        </div>

        {query.trim() !== "" && searchError && <ErrorMessage message={searchError} />}

        {showNoCandidates && (
          <p className="mt-2 text-sm text-gray-500">該当する案件が見つかりませんでした。</p>
        )}

        {query.trim() !== "" && candidates.length > 0 && (
          <div className="mt-2 max-w-md">
            <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white shadow-sm">
              {candidates.map((project) => (
                <li key={project.notion_page_id}>
                  <button
                    type="button"
                    onClick={() => handleSelectProject(project)}
                    className="w-full px-4 py-2 text-left text-sm text-gray-900 hover:bg-gray-50"
                  >
                    <span className="font-medium">{project.project_name}</span>
                    {project.status && (
                      <span className="ml-2 text-xs text-gray-500">{project.status}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
            {hiddenMatchCount > 0 && (
              <p className="mt-1 text-xs text-gray-500">
                他に{hiddenMatchCount}件該当しています。案件名をさらに絞り込んでください。
              </p>
            )}
          </div>
        )}
      </section>

      {selectedProject && (
        <section className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h2 className="mb-2 text-lg font-semibold text-gray-900">選択中の案件</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-gray-500">案件名</dt>
            <dd className="text-gray-900">{selectedProject.project_name}</dd>
            <dt className="text-gray-500">ステータス</dt>
            <dd className="text-gray-900">{selectedProject.status ?? "-"}</dd>
            <dt className="text-gray-500">提案サービス</dt>
            <dd className="text-gray-900">
              {selectedProject.proposed_services.length > 0
                ? selectedProject.proposed_services.join("、")
                : "-"}
            </dd>
          </dl>
        </section>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">書類の種類</h2>
        <div className="flex gap-6">
          {DOCUMENT_CATEGORIES.map((option) => (
            <label key={option} className="flex items-center gap-2 text-sm text-gray-900">
              <input
                type="radio"
                name="category"
                value={option}
                checked={category === option}
                onChange={() => setCategory(option)}
              />
              {option}
            </label>
          ))}
        </div>
      </section>

      {selectedProject && category === "見積書" && (
        <section>
          <h2 className="mb-3 text-lg font-semibold text-gray-900">詳細情報（任意）</h2>
          <p className="mb-3 text-sm text-gray-500">
            未入力の項目は案件データ（Notion）の値をそのまま使用します。
          </p>
          <div className="grid max-w-2xl grid-cols-1 gap-4 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-sm text-gray-700">
              クライアント名
              <input
                type="text"
                value={clientNameOverride}
                onChange={(event) => setClientNameOverride(event.target.value)}
                className="rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-gray-700">
              商材名
              <input
                type="text"
                value={serviceName}
                onChange={(event) => setServiceName(event.target.value)}
                placeholder="例: リピッテ"
                className="rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-gray-700">
              初期費用
              <input
                type="text"
                value={initialFee}
                onChange={(event) => setInitialFee(event.target.value)}
                placeholder="例: 100,000円"
                className="rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-gray-700">
              月額費用
              <input
                type="text"
                value={monthlyFee}
                onChange={(event) => setMonthlyFee(event.target.value)}
                placeholder="例: 30,000円"
                className="rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-gray-700">
              作成者
              <input
                type="text"
                value={creatorName}
                onChange={(event) => setCreatorName(event.target.value)}
                placeholder="例: Kanazawa"
                className="rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
              <span className="text-xs text-gray-400">
                見積書の「担当」欄と見積書NOの先頭1文字に使われます。見積書NOをアルファベット表記にしたい場合は半角英字（ローマ字）で入力してください（自動変換はされません）。
              </span>
            </label>
            <label className="flex flex-col gap-1 text-sm text-gray-700 sm:col-span-2">
              備考
              <textarea
                value={memo}
                onChange={(event) => setMemo(event.target.value)}
                rows={2}
                className="rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>
          </div>
        </section>
      )}

      <section>
        <button
          type="button"
          onClick={handleGenerate}
          disabled={!selectedProject || generating}
          className="rounded-md bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:bg-gray-300"
        >
          {generating ? "生成中..." : "書類を生成"}
        </button>

        {generateError && (
          <div className="mt-4">
            <ErrorMessage message={generateError} />
          </div>
        )}

        {documentNotes && (
          <div className="mt-4 rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-800">
            「{generatedFilename}」の生成・ダウンロードが完了しました。
          </div>
        )}

        {specificNotes.length > 0 && (
          <div className="mt-3 rounded-lg border-2 border-red-300 bg-red-50 p-4 text-sm text-red-900">
            <p className="font-bold">⚠ 送付前に必ずご確認ください</p>
            <ul className="mt-1 list-disc pl-5">
              {specificNotes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        )}

        {baselineNote && (
          <p className="mt-3 text-xs text-gray-500">{baselineNote}</p>
        )}
      </section>

      {selectedProject && category === "見積書" && (
        <section className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h2 className="mb-2 text-lg font-semibold text-gray-900">承認リクエストを送信</h2>
          <p className="mb-3 text-sm text-gray-500">
            見積書をDriveの一時格納フォルダへ保存し、Googleドライブ純正の「承認をリクエスト」
            機能で承認者へ送信します。
          </p>

          {!driveConnected ? (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
              承認リクエストを送信するには、先に自分のDriveアカウントを連携する必要があります。
              <Link href="/settings/drive" className="ml-1 underline">
                設定画面でDrive連携を行う
              </Link>
            </div>
          ) : approvers.length === 0 ? (
            <p className="text-sm text-gray-500">
              承認者が登録されていません。管理者に見積書承認者管理での登録を依頼してください。
            </p>
          ) : (
            <div className="flex flex-col gap-3">
              <label className="flex flex-col gap-1 text-sm text-gray-700">
                承認者
                <select
                  value={approverEmail}
                  onChange={(event) => setApproverEmail(event.target.value)}
                  className="w-full max-w-xs rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
                >
                  <option value="">選択してください</option>
                  {approvers.map((approver) => (
                    <option key={approver.id} value={approver.email}>
                      {approver.name}
                      {approver.title ? `（${approver.title}）` : ""}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-sm text-gray-700">
                メッセージ(任意)
                <textarea
                  value={approvalMessage}
                  onChange={(event) => setApprovalMessage(event.target.value)}
                  rows={2}
                  className="w-full max-w-md rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
                />
              </label>
              <div>
                <button
                  type="button"
                  onClick={handleRequestApproval}
                  disabled={requestingApproval || !approverEmail}
                  className="rounded-md bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:bg-gray-300"
                >
                  {requestingApproval ? "送信中..." : "承認リクエストを送信"}
                </button>
              </div>
              {approvalError && <ErrorMessage message={approvalError} />}
              {approvalSuccess && (
                <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-800">
                  承認リクエストを送信しました。承認され次第、送付済みフォルダへ移動され通知されます。
                </div>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
