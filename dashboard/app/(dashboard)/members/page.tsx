import ErrorMessage from "@/components/ErrorMessage";
import MemberScoreChart from "@/components/charts/MemberScoreChart";
import { MembersPerformanceResponse, getErrorMessage, getMembersPerformance } from "@/lib/backend";
import { formatPercent } from "@/lib/format";

// バックエンドの最新データを毎リクエスト取得するため、静的プリレンダリングを無効化する。
export const dynamic = "force-dynamic";

export default async function MembersPage() {
  let performance: MembersPerformanceResponse;
  try {
    performance = await getMembersPerformance();
  } catch (error) {
    return <ErrorMessage message={getErrorMessage(error)} />;
  }

  const chartData = performance.members
    .filter((member) => member.overall_score !== null)
    .map((member) => ({ name: member.member, value: member.overall_score as number }));

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">メンバー別実績</h1>
        <p className="mt-1 text-sm text-gray-500">基準日: {performance.as_of}</p>
      </div>

      {performance.notes.length > 0 && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-4 text-sm text-yellow-800">
          <p className="font-medium">注記</p>
          <ul className="mt-1 list-disc pl-5">
            {performance.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">総合スコア（overall_score）</h2>
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          {chartData.length === 0 ? (
            <p className="text-sm text-gray-500">総合スコアが算出可能なメンバーがいません（データ不足）。</p>
          ) : (
            <MemberScoreChart data={chartData} />
          )}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-gray-900">実績一覧</h2>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="data-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left">メンバー</th>
                <th className="text-right">接触件数（ボリューム）</th>
                <th className="text-right">ボリュームスコア</th>
                <th className="text-right">受注率（クオリティ）</th>
                <th className="text-right">期限遵守率（スピード）</th>
                <th className="text-right">総合スコア</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {performance.members.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    データがありません
                  </td>
                </tr>
              ) : (
                performance.members.map((member) => (
                  <tr key={member.member}>
                    <td>{member.member}</td>
                    <td className="text-right">
                      {member.volume_contact_count.toLocaleString("ja-JP")}
                    </td>
                    <td className="text-right">{formatPercent(member.volume_score)}</td>
                    <td className="text-right">
                      {formatPercent(member.quality_win_rate)}
                    </td>
                    <td className="text-right">
                      {formatPercent(member.speed_compliance_rate)}
                    </td>
                    <td className="text-right font-semibold">
                      {formatPercent(member.overall_score)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-gray-400">
          「-」はデータ不足により未確定であることを示します（0%とは異なります）。
        </p>
      </section>
    </div>
  );
}
