import ErrorMessage from "@/components/ErrorMessage";
import DatePicker from "@/components/DatePicker";
import { DailyReport, getDailyReport, getErrorMessage } from "@/lib/backend";
import { formatYen } from "@/lib/format";
import { todayDateStringJst } from "@/lib/date";

// バックエンドの最新データを毎リクエスト取得するため、静的プリレンダリングを無効化する。
export const dynamic = "force-dynamic";

export default async function ReportsPage(props: PageProps<"/reports">) {
  const searchParams = await props.searchParams;
  const date = typeof searchParams.date === "string" ? searchParams.date : todayDateStringJst();

  let report: DailyReport;
  try {
    report = await getDailyReport(date);
  } catch (error) {
    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-gray-900">日報</h1>
          <DatePicker basePath="/reports" date={date} />
        </div>
        <ErrorMessage message={getErrorMessage(error)} />
      </div>
    );
  }

  const actionTypes = Array.from(
    new Set(report.member_summaries.flatMap((m) => Object.keys(m.counts_by_type)))
  );

  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">日報</h1>
          <p className="mt-1 text-sm text-gray-500">
            対象日: {report.report_date}（翌営業日: {report.next_business_day}）
          </p>
        </div>
        <DatePicker basePath="/reports" date={date} />
      </div>

      {report.notes.length > 0 && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-4 text-sm text-yellow-800">
          <p className="font-medium">注記</p>
          <ul className="mt-1 list-disc pl-5">
            {report.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">メンバー別アクション件数</h2>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="data-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left">メンバー</th>
                {actionTypes.map((type) => (
                  <th key={type} className="px-4 py-2 text-right font-medium text-gray-500">
                    {type}
                  </th>
                ))}
                <th className="text-right">合計</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {report.member_summaries.length === 0 ? (
                <tr>
                  <td colSpan={actionTypes.length + 2}>
                    データがありません
                  </td>
                </tr>
              ) : (
                report.member_summaries.map((member) => (
                  <tr key={member.member}>
                    <td>{member.member}</td>
                    {actionTypes.map((type) => (
                      <td key={type} className="px-4 py-2 text-right text-gray-900">
                        {(member.counts_by_type[type] ?? 0).toLocaleString("ja-JP")}
                      </td>
                    ))}
                    <td className="text-right font-semibold">
                      {member.total.toLocaleString("ja-JP")}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">本日の新規獲得案件</h2>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="data-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left">クライアント</th>
                <th className="text-left">提案サービス</th>
                <th className="text-right">初期費用</th>
                <th className="text-right">月額費用</th>
                <th className="text-left">担当者</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {report.new_projects.length === 0 ? (
                <tr>
                  <td colSpan={5}>
                    本日の新規獲得案件はありません
                  </td>
                </tr>
              ) : (
                report.new_projects.map((project) => (
                  <tr key={project.client_name}>
                    <td>{project.client_name}</td>
                    <td>{project.proposed_services.join("、")}</td>
                    <td className="text-right">{formatYen(project.initial_fee)}</td>
                    <td className="text-right">{formatYen(project.monthly_fee)}</td>
                    <td>{project.assignee}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">
          翌営業日（{report.next_business_day}）の次回アクション予定
        </h2>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="data-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left">クライアント</th>
                <th className="text-left">担当者</th>
                <th className="text-left">次回アクション予定日</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {report.upcoming_actions.length === 0 ? (
                <tr>
                  <td colSpan={3}>
                    予定はありません
                  </td>
                </tr>
              ) : (
                report.upcoming_actions.map((action, index) => (
                  <tr key={`${action.client_name}-${index}`}>
                    <td>{action.client_name}</td>
                    <td>{action.assignee}</td>
                    <td>{action.next_action_date}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <p className="text-xs text-gray-400">
        ステータス変更履歴（status_changes）は変更履歴データが未整備のため常に空です。
      </p>
    </div>
  );
}
