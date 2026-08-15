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

export default async function DashboardPage() {
  let summary: DashboardSummary;
  try {
    summary = await getDashboardSummary();
  } catch (error) {
    return <ErrorMessage message={getErrorMessage(error)} />;
  }

  const { forecast, notes, status_breakdown, totals, as_of } = summary;

  const categoryCounts = new Map<string, number>();
  for (const item of status_breakdown) {
    categoryCounts.set(item.category, (categoryCounts.get(item.category) ?? 0) + item.count);
  }
  const categoryChartData = Array.from(categoryCounts.entries()).map(([name, value]) => ({
    name,
    value,
  }));

  const summaryCards = [
    { label: "案件数", value: totals.project_count },
    { label: "契約済", value: totals.confirmed_count },
    { label: "進行中", value: totals.active_count },
    { label: "失注", value: totals.lost_count },
    { label: "解約", value: totals.cancelled_count },
  ];

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">全社ダッシュボード</h1>
        <p className="mt-1 text-sm text-gray-500">基準日: {formatDate(as_of)}</p>
      </div>

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

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">営業ステータス内訳</h2>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
            <StatusCategoryChart data={categoryChartData} colors={CATEGORY_CHART_COLORS} />
          </div>
          <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-2 text-left font-medium text-gray-500">ステータス</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-500">区分</th>
                  <th className="px-4 py-2 text-right font-medium text-gray-500">件数</th>
                  <th className="px-4 py-2 text-right font-medium text-gray-500">初期費用計</th>
                  <th className="px-4 py-2 text-right font-medium text-gray-500">月額計</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {status_breakdown.map((item) => (
                  <tr key={item.status}>
                    <td className="px-4 py-2 text-gray-900">{item.status}</td>
                    <td className="px-4 py-2">
                      <span
                        className={`rounded px-2 py-0.5 text-xs font-medium ${
                          CATEGORY_BADGE_CLASSES[item.category] ?? "bg-gray-100 text-gray-700"
                        }`}
                      >
                        {item.category}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right text-gray-900">{item.count.toLocaleString("ja-JP")}</td>
                    <td className="px-4 py-2 text-right text-gray-900">{formatYen(item.initial_fee_sum)}</td>
                    <td className="px-4 py-2 text-right text-gray-900">{formatYen(item.monthly_fee_sum)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
