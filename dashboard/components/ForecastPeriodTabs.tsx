"use client";

import { useState } from "react";
import ForecastBarChart from "@/components/charts/ForecastBarChart";
import type { ForecastPeriod } from "@/lib/backend";
import { formatDateRange, formatYen } from "@/lib/format";

type ForecastPeriodKey = "quarter" | "half" | "year";

const FORECAST_PERIOD_LABELS: Record<ForecastPeriodKey, string> = {
  quarter: "クオーター着地予測",
  half: "半期着地予測",
  year: "通期着地予測",
};

const FORECAST_PERIOD_ORDER: ForecastPeriodKey[] = ["quarter", "half", "year"];

interface ForecastPeriodTabsProps {
  periods: Record<ForecastPeriodKey, ForecastPeriod>;
}

// クオーター/半期/通期は累積（半期はクオーター分を、通期は半期分を含む）ため、契約日/
// 予想契約日が入っている案件が現在の期間に集中していると3期間とも同じ金額になる
// （バグではない、src/api/dashboard_service.pyのbuild_dashboard_summary docstring参照）。
// 3枚の同じグラフを常に並べて表示すると冗長でバグに見えるため、タブ切り替え式にして
// 常時表示は1期間分のみにする（2026-08-15、金沢さんの指摘を受けて変更）。
export default function ForecastPeriodTabs({ periods }: ForecastPeriodTabsProps) {
  const [activeTab, setActiveTab] = useState<ForecastPeriodKey>("quarter");
  const period = periods[activeTab];

  const initialFeeData = [
    { name: "Min", value: period.min.initial_fee },
    { name: "Expected", value: period.expected.initial_fee },
    { name: "Max", value: period.max.initial_fee },
  ];
  const mrrData = [
    { name: "Min", value: period.min.mrr },
    { name: "Expected", value: period.expected.mrr },
    { name: "Max", value: period.max.mrr },
  ];

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex gap-1 rounded-lg border border-gray-200 bg-gray-50 p-1">
          {FORECAST_PERIOD_ORDER.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setActiveTab(key)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                activeTab === key
                  ? "bg-white text-gray-900 shadow-sm"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              {FORECAST_PERIOD_LABELS[key]}
            </button>
          ))}
        </div>
        <span className="text-sm text-gray-500">{formatDateRange(period.range.start, period.range.end)}</span>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h4 className="mb-2 text-sm font-medium text-gray-700">初期費用</h4>
          <ForecastBarChart data={initialFeeData} color="#2563eb" />
          <dl className="mt-2 grid grid-cols-3 gap-2 text-center text-xs text-gray-600">
            {initialFeeData.map((item) => (
              <div key={item.name}>
                <dt>{item.name}</dt>
                <dd className="font-semibold text-gray-900">{formatYen(item.value)}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h4 className="mb-2 text-sm font-medium text-gray-700">MRR</h4>
          <ForecastBarChart data={mrrData} color="#16a34a" />
          <dl className="mt-2 grid grid-cols-3 gap-2 text-center text-xs text-gray-600">
            {mrrData.map((item) => (
              <div key={item.name}>
                <dt>{item.name}</dt>
                <dd className="font-semibold text-gray-900">{formatYen(item.value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </div>
  );
}
