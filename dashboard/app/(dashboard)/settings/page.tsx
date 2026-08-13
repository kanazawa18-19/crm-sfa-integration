"use client";

import { useEffect, useState } from "react";
import ErrorMessage from "@/components/ErrorMessage";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

interface RevenueTargetSheetSettings {
  configured: boolean;
  pointer: {
    spreadsheet_id: string;
    mrr_sheet_name: string | null;
    unit_count_sheet_name: string | null;
  } | null;
  updated_at: string | null;
}

interface SaveResult {
  pointer: {
    spreadsheet_id: string;
    mrr_sheet_name: string | null;
    unit_count_sheet_name: string | null;
  };
  updated_at: string;
  validation_success: boolean;
  validation_error: string | null;
  mrr_month_count: number | null;
  unit_count_month_count: number | null;
}

// null（対応するsheet_nameが未設定）と、数値（設定済み・実際に読み込めた月数）を
// 明確に区別する文言にする。「件」は本アプリの他画面（販売件数レポート等）で
// レコード数を表す単位として使われているため、月数を表す本文言には使わない（finding #7）。
function formatTargetMonthCountText(monthCount: number | null): string {
  return monthCount !== null
    ? `${monthCount}ヶ月分のデータを読み込みました`
    : "未設定（このソースでは追跡しません）";
}

export default function SettingsPage() {
  const [spreadsheetUrl, setSpreadsheetUrl] = useState("");
  const [mrrSheetName, setMrrSheetName] = useState("");
  const [unitCountSheetName, setUnitCountSheetName] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveResult, setSaveResult] = useState<SaveResult | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadSettings() {
      try {
        const response = await fetch("/api/settings/revenue-target-sheet", {
          redirect: "manual",
        });
        if (isSessionExpiredResponse(response)) {
          throw new Error(SESSION_EXPIRED_MESSAGE);
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail ?? "設定の取得に失敗しました");
        }
        const data = (await response.json()) as RevenueTargetSheetSettings;
        if (cancelled) {
          return;
        }
        if (data.pointer) {
          setSpreadsheetUrl(data.pointer.spreadsheet_id);
          setMrrSheetName(data.pointer.mrr_sheet_name ?? "");
          setUnitCountSheetName(data.pointer.unit_count_sheet_name ?? "");
        }
        setUpdatedAt(data.updated_at);
      } catch (error) {
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : "設定の取得に失敗しました");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    loadSettings();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSaveResult(null);

    try {
      const response = await fetch("/api/settings/revenue-target-sheet", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          spreadsheet_url_or_id: spreadsheetUrl,
          mrr_sheet_name: mrrSheetName.trim() || null,
          unit_count_sheet_name: unitCountSheetName.trim() || null,
        }),
        redirect: "manual",
      });

      if (isSessionExpiredResponse(response)) {
        throw new Error(SESSION_EXPIRED_MESSAGE);
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? "設定の保存に失敗しました");
      }

      const result = (await response.json()) as SaveResult;
      setSaveResult(result);
      setUpdatedAt(result.updated_at);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "設定の保存に失敗しました");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">設定</h1>
        <p className="mt-1 text-sm text-gray-500">
          月次・クオーター目標値の情報源となる、事業計画スプレッドシートを設定します。
          目標値そのものはこのシステムに保存されず、レポート生成のたびにスプレッドシートを直接読みに行きます。
        </p>
      </div>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">事業計画スプレッドシート連携</h2>

        {loading && <p className="text-sm text-gray-500">読み込み中...</p>}

        {!loading && loadError && <ErrorMessage message={loadError} />}

        {!loading && (
          <div className="flex max-w-xl flex-col gap-4">
            {updatedAt && (
              <p className="text-xs text-gray-500">
                最終更新: {new Date(updatedAt).toLocaleString("ja-JP")}
              </p>
            )}

            <label className="flex flex-col gap-1 text-sm text-gray-900">
              スプレッドシートURL（またはID）
              <input
                type="text"
                value={spreadsheetUrl}
                onChange={(event) => setSpreadsheetUrl(event.target.value)}
                placeholder="https://docs.google.com/spreadsheets/d/xxxxx/edit"
                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>

            <label className="flex flex-col gap-1 text-sm text-gray-900">
              MRR目標シート名（任意）
              <input
                type="text"
                value={mrrSheetName}
                onChange={(event) => setMrrSheetName(event.target.value)}
                placeholder="✳︎営業部事業計画（月額ver）"
                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>

            <label className="flex flex-col gap-1 text-sm text-gray-900">
              販売数目標シート名（任意）
              <input
                type="text"
                value={unitCountSheetName}
                onChange={(event) => setUnitCountSheetName(event.target.value)}
                placeholder="✳︎販売計画"
                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-gray-500 focus:outline-none"
              />
            </label>

            <div>
              <button
                type="button"
                onClick={handleSave}
                disabled={!spreadsheetUrl.trim() || saving}
                className="rounded-md bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:bg-gray-300"
              >
                {saving ? "保存中..." : "保存してテスト"}
              </button>
            </div>

            {saveError && <ErrorMessage message={saveError} />}

            {saveResult && saveResult.validation_success && (
              <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-800">
                <p className="font-bold">✅ 保存しました。</p>
                {/* mrr_month_count/unit_count_month_countはnull（未設定・このソースでは
                    追跡しない）と0以上の数値（設定済み・Nヶ月分読み込み済み）を明確に区別する
                    （BLOCKER回帰確認: finding #2）。両方未設定のまま保存した場合でも、
                    どちらの指標もこのシートからは取得されないことが分かるようにする。 */}
                <ul className="mt-1 list-disc pl-5">
                  <li>MRR目標: {formatTargetMonthCountText(saveResult.mrr_month_count)}</li>
                  <li>販売数目標: {formatTargetMonthCountText(saveResult.unit_count_month_count)}</li>
                </ul>
              </div>
            )}

            {saveResult && !saveResult.validation_success && (
              <div className="rounded-lg border-2 border-red-300 bg-red-50 p-4 text-sm text-red-900">
                <p className="font-bold">
                  ⚠ 設定は保存しましたが、シートの読み取りに失敗しました
                </p>
                <p className="mt-1">{saveResult.validation_error}</p>
                {/* POSTハンドラのdocstring（src/api/app.py）の通り、保存自体は取り消さない
                    （検証失敗時のみ）。日報・週報バッチもこの状態では環境変数へフォールバック
                    し続けるため、レポート生成自体は止まらないことを明示する（finding #7）。 */}
                <p className="mt-2">
                  保存自体は完了しています。この状態のままレポートは既存の目標値（環境変数）を使い続けます。
                </p>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
