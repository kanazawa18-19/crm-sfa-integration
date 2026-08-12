import ErrorMessage from "@/components/ErrorMessage";
import DatePicker from "@/components/DatePicker";
import {
  ManagerAlertEntry,
  ManagerAlertsResponse,
  getErrorMessage,
  getManagerAlerts,
} from "@/lib/backend";

// バックエンドの最新データを毎リクエスト取得するため、静的プリレンダリングを無効化する。
export const dynamic = "force-dynamic";

function todayDateString(): string {
  return new Date().toISOString().slice(0, 10);
}

interface AlertSection {
  key: keyof ManagerAlertsResponse["alerts"];
  title: string;
  description: string;
  accentClassName: string;
}

const ALERT_SECTIONS: AlertSection[] = [
  {
    key: "lost",
    title: "失注",
    description: "失注が確定した案件です。",
    accentClassName: "border-red-200 bg-red-50 text-red-700",
  },
  {
    key: "lost_candidate",
    title: "失注候補",
    description: "確度Dかつ進行中ステータスを代理指標として抽出した案件です。実際のステータス変更ではありません。",
    accentClassName: "border-orange-200 bg-orange-50 text-orange-700",
  },
  {
    key: "stalled",
    title: "停滞案件",
    description: "次回アクション日ベースの独自指標で停滞と判定された案件です。",
    accentClassName: "border-yellow-200 bg-yellow-50 text-yellow-700",
  },
  {
    key: "won",
    title: "契約成立",
    description: "契約が成立した案件です。",
    accentClassName: "border-green-200 bg-green-50 text-green-700",
  },
];

function AlertTable({ entries }: { entries: ManagerAlertEntry[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
      <table className="min-w-full divide-y divide-gray-200 text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="px-4 py-2 text-left font-medium text-gray-500">案件名</th>
            <th className="px-4 py-2 text-left font-medium text-gray-500">担当者</th>
            <th className="px-4 py-2 text-left font-medium text-gray-500">ステータス</th>
            <th className="px-4 py-2 text-left font-medium text-gray-500">確度</th>
            <th className="px-4 py-2 text-left font-medium text-gray-500">次回アクション予定日</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {entries.length === 0 ? (
            <tr>
              <td className="px-4 py-3 text-gray-500" colSpan={5}>
                該当なし
              </td>
            </tr>
          ) : (
            entries.map((entry) => (
              <tr key={entry.notion_page_id}>
                <td className="px-4 py-2 text-gray-900">{entry.project_name}</td>
                <td className="px-4 py-2 text-gray-900">{entry.assignee}</td>
                <td className="px-4 py-2 text-gray-900">
                  {entry.status}
                  {entry.is_proxy && (
                    <span
                      title="実データにこの状態を表すステータス値が存在しないため、他の指標から代理判定した結果です。実際のステータス変更ではありません。"
                      className="ml-2 rounded bg-orange-200 px-1.5 py-0.5 text-xs font-semibold text-orange-800"
                    >
                      代理指標
                    </span>
                  )}
                </td>
                <td className="px-4 py-2 text-gray-900">{entry.confidence}</td>
                <td className="px-4 py-2 text-gray-900">{entry.next_action_date ?? "未設定"}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

export default async function AlertsPage(props: PageProps<"/alerts">) {
  const searchParams = await props.searchParams;
  // DatePickerコンポーネントは共通実装でURLクエリパラメータ名を常に"date"に固定しているため、
  // reports/page.tsxと同様に"date"で受け取り、バックエンドへは"as_of"として渡す。
  const asOf = typeof searchParams.date === "string" ? searchParams.date : todayDateString();

  let alerts: ManagerAlertsResponse;
  try {
    alerts = await getManagerAlerts(asOf);
  } catch (error) {
    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-gray-900">マネージャー通知</h1>
          <DatePicker basePath="/alerts" date={asOf} />
        </div>
        <ErrorMessage message={getErrorMessage(error)} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">マネージャー通知</h1>
          <p className="mt-1 text-sm text-gray-500">
            基準日: {alerts.as_of}（停滞判定の基準: {alerts.stalled_days_threshold}日）
          </p>
        </div>
        <DatePicker basePath="/alerts" date={asOf} />
      </div>

      {alerts.notes.length > 0 && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-4 text-sm text-yellow-800">
          <p className="font-medium">注記</p>
          <ul className="mt-1 list-disc pl-5">
            {alerts.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {ALERT_SECTIONS.map((section) => (
          <div
            key={section.key}
            className={`rounded-lg border p-4 ${section.accentClassName}`}
          >
            <p className="text-sm font-medium">{section.title}</p>
            <p className="mt-1 text-3xl font-bold">
              {alerts.counts[section.key].toLocaleString("ja-JP")}
            </p>
          </div>
        ))}
      </div>

      {ALERT_SECTIONS.map((section) => (
        <section key={section.key}>
          <h2 className="mb-1 text-lg font-semibold text-gray-900">{section.title}</h2>
          <p className="mb-3 text-sm text-gray-500">{section.description}</p>
          <AlertTable entries={alerts.alerts[section.key]} />
        </section>
      ))}
    </div>
  );
}
