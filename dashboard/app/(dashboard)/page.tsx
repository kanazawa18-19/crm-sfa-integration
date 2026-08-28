import { Suspense } from "react";
import ErrorMessage from "@/components/ErrorMessage";
import ForecastPeriodTabs from "@/components/ForecastPeriodTabs";
import StatusCategoryChart from "@/components/charts/StatusCategoryChart";
import { DashboardSummary, getDashboardSummary, getErrorMessage } from "@/lib/backend";
import { formatDate, formatYen } from "@/lib/format";

// バックエンドの最新データを毎リクエスト取得するため、静的プリレンダリングを無効化する。
export const dynamic = "force-dynamic";

const CATEGORY_BADGE_CLASSES: Record<string, string> = {
  契約済: "bg-green-100 text-green-800",
  進行中: "bg-blue-100 text-blue-800",
  失注: "bg-red-100 text-red-800",
  解約: "bg-gray-200 text-gray-700",
};

const CATEGORY_CHART_COLORS: Record<string, string> = {
  契約済: "#16a34a",
  進行中: "#2563eb",
  失注: "#dc2626",
  解約: "#6b7280",
};

// getDashboardSummary()（Notion全案件を集計するため重い。案件数次第ではコールドキャッシュ時
// 100秒超かかることを実測済み）の完了を待たずに見出しを即座に描画できるよう、Promiseは
// ページ本体では待たず各セクションへそのまま渡す（呼び出しは1回のみ・下記4箇所で共有する
// ため、バックエンドへのリクエスト回数は増えない）。
//
// 注意（2026-08-17、動物チームレビュー指摘）: 4箇所とも同じ1つのPromiseをawaitしているため、
// バックエンド呼び出しが1回で済む代わりに、実際には4セクションがほぼ同時に解決・表示切替
// される（段階的に順々表示されるわけではない）。Suspenseによる分割が効いているのは主に
// 「即座に描画できる見出し」と「summaryPromiseに依存するボディ全体」の間であり、
// ボディ内の4セクション間の分割自体に体感速度上の恩恵はほぼ無い。それでもセクションごとに
// 分けているのは、各セクションの表示崩れ・エラーを他セクションから独立させるため。
export default function DashboardPage() {
  const summaryPromise = getDashboardSummary();

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">全社ダッシュボード</h1>
        <Suspense fallback={<AsOfDateSkeleton />}>
          <AsOfDate summaryPromise={summaryPromise} />
        </Suspense>
      </div>

      <Suspense fallback={<SummaryCardsSkeleton />}>
        <SummaryCardsSection summaryPromise={summaryPromise} />
      </Suspense>

      <Suspense fallback={<ForecastSectionSkeleton />}>
        <ForecastSection summaryPromise={summaryPromise} />
      </Suspense>

      <Suspense fallback={<StatusBreakdownSkeleton />}>
        <StatusBreakdownSection summaryPromise={summaryPromise} />
      </Suspense>
    </div>
  );
}

interface SectionProps {
  summaryPromise: Promise<DashboardSummary>;
}

type SummaryResult = { summary: DashboardSummary } | { errorMessage: string };

// 4セクションで重複していたawait+try/catchを1箇所に集約する共通ヘルパー。
// エラーメッセージ自体はここで作るが、実際に画面へ表示するかどうか（＝<ErrorMessage>を
// 描画するかどうか）は呼び出し元が決める。全セクションがそれぞれ<ErrorMessage>を描画すると
// 同じエラーが最大4重に表示されてしまうため、表示責務はSummaryCardsSection 1箇所のみに
// 集約している（2026-08-17、動物チームレビュー指摘対応）。
async function resolveSummary(summaryPromise: Promise<DashboardSummary>): Promise<SummaryResult> {
  try {
    return { summary: await summaryPromise };
  } catch (error) {
    return { errorMessage: getErrorMessage(error) };
  }
}

async function AsOfDate({ summaryPromise }: SectionProps) {
  const result = await resolveSummary(summaryPromise);
  // エラー表示はSummaryCardsSectionに集約しているため、ここでは基準日を空欄にするのみで
  // 追加のエラーメッセージは出さない。
  if ("errorMessage" in result) {
    return <p className="mt-1 text-sm text-gray-500">基準日: -</p>;
  }
  return <p className="mt-1 text-sm text-gray-500">基準日: {formatDate(result.summary.as_of)}</p>;
}

function AsOfDateSkeleton() {
  return (
    <div className="mt-1 flex flex-col gap-1">
      <div className="h-5 w-40 animate-pulse rounded bg-gray-200" />
      {/* コールドキャッシュ時は実測で100秒超かかることがあるため、進捗の手がかりが
          一切ないまま固まって見えないよう一言添える（2026-08-17、動物チームレビュー指摘対応）。 */}
      <p className="text-xs text-gray-400">
        読み込み中です。データ量によっては表示まで数十秒〜数分かかる場合があります。
      </p>
    </div>
  );
}

async function SummaryCardsSection({ summaryPromise }: SectionProps) {
  const result = await resolveSummary(summaryPromise);
  // 4セクション中この1箇所のみでエラーメッセージを表示する（他はresolveSummaryのコメント参照）。
  if ("errorMessage" in result) {
    return <ErrorMessage message={result.errorMessage} />;
  }

  const { totals } = result.summary;
  const summaryCards = [
    { label: "案件数", value: totals.project_count },
    { label: "契約済", value: totals.confirmed_count },
    { label: "進行中", value: totals.active_count },
    { label: "失注", value: totals.lost_count },
    { label: "解約", value: totals.cancelled_count },
  ];

  return (
    <section>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        {summaryCards.map((card) => (
          <div key={card.label} className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
            <p className="text-sm text-gray-500">{card.label}</p>
            <p className="mt-1 text-2xl font-bold text-gray-900">{card.value.toLocaleString("ja-JP")}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function SummaryCardsSkeleton() {
  return (
    <section>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
            <div className="h-4 w-12 animate-pulse rounded bg-gray-200" />
            <div className="mt-2 h-7 w-16 animate-pulse rounded bg-gray-200" />
          </div>
        ))}
      </div>
    </section>
  );
}

async function ForecastSection({ summaryPromise }: SectionProps) {
  const result = await resolveSummary(summaryPromise);
  // エラー表示はSummaryCardsSectionに集約しているため、ここでは何も描画しない。
  if ("errorMessage" in result) {
    return null;
  }

  const { forecast, notes } = result.summary;

  return (
    <section>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-lg font-semibold text-gray-900">着地予測</h2>
        <p className="text-xs text-gray-500">会計年度は12月始まり・11月末</p>
      </div>

      {notes.length > 0 && (
        <div className="mb-3 rounded-lg border border-yellow-200 bg-yellow-50 p-4 text-sm text-yellow-800">
          <p className="font-medium">注記</p>
          <ul className="mt-1 list-disc pl-5">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="mb-4 text-xs text-gray-500">
        半期・通期の実績にはクオーター分も含まれます（3期間は累積であり、独立した数字ではありません）。
      </p>

      <ForecastPeriodTabs
        periods={{ quarter: forecast.quarter, half: forecast.half, year: forecast.year }}
      />
    </section>
  );
}

function ForecastSectionSkeleton() {
  return (
    <section>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-lg font-semibold text-gray-900">着地予測</h2>
        <p className="text-xs text-gray-500">会計年度は12月始まり・11月末</p>
      </div>
      <div className="mb-4 h-8 w-64 animate-pulse rounded-lg bg-gray-200" />
      <div className="h-64 animate-pulse rounded-lg border border-gray-200 bg-gray-100" />
    </section>
  );
}

async function StatusBreakdownSection({ summaryPromise }: SectionProps) {
  const result = await resolveSummary(summaryPromise);
  // エラー表示はSummaryCardsSectionに集約しているため、ここでは何も描画しない。
  if ("errorMessage" in result) {
    return null;
  }

  const { status_breakdown } = result.summary;

  const categoryCounts = new Map<string, number>();
  for (const item of status_breakdown) {
    categoryCounts.set(item.category, (categoryCounts.get(item.category) ?? 0) + item.count);
  }
  const categoryChartData = Array.from(categoryCounts.entries()).map(([name, value]) => ({
    name,
    value,
  }));

  return (
    <section>
      <h2 className="mb-3 text-lg font-semibold text-gray-900">営業ステータス内訳</h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <StatusCategoryChart data={categoryChartData} colors={CATEGORY_CHART_COLORS} />
        </div>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="data-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left">ステータス</th>
                <th className="text-left">区分</th>
                <th className="text-right">件数</th>
                <th className="text-right">初期費用計</th>
                <th className="text-right">月額計</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {status_breakdown.map((item) => (
                <tr key={item.status}>
                  <td>{item.status}</td>
                  <td>
                    <span
                      className={`rounded px-2 py-0.5 text-xs font-medium ${
                        CATEGORY_BADGE_CLASSES[item.category] ?? "bg-gray-100 text-gray-700"
                      }`}
                    >
                      {item.category}
                    </span>
                  </td>
                  <td className="text-right">{item.count.toLocaleString("ja-JP")}</td>
                  <td className="text-right">{formatYen(item.initial_fee_sum)}</td>
                  <td className="text-right">{formatYen(item.monthly_fee_sum)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function StatusBreakdownSkeleton() {
  return (
    <section>
      <h2 className="mb-3 text-lg font-semibold text-gray-900">営業ステータス内訳</h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="h-[292px] animate-pulse rounded-lg border border-gray-200 bg-gray-100" />
        <div className="h-[292px] animate-pulse rounded-lg border border-gray-200 bg-gray-100" />
      </div>
    </section>
  );
}
