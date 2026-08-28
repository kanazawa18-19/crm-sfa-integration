import ErrorMessage from "@/components/ErrorMessage";
import { Task, TasksResponse, getErrorMessage, getTasks } from "@/lib/backend";

// バックエンドの最新データを毎リクエスト取得するため、静的プリレンダリングを無効化する。
export const dynamic = "force-dynamic";

function joinOrDash(values: string[]): string {
  return values.length > 0 ? values.join("、") : "-";
}

function taskRowClassName(task: Task): string {
  return task.is_overdue ? "bg-red-50" : "";
}

export default async function TasksPage() {
  let tasks: TasksResponse;
  try {
    tasks = await getTasks();
  } catch (error) {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-bold text-gray-900">タスク一覧</h1>
        <ErrorMessage message={getErrorMessage(error)} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">タスク一覧</h1>
        <p className="mt-1 text-sm text-gray-500">基準日: {tasks.as_of}</p>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:max-w-md">
        <div className="rounded-lg border border-red-200 bg-red-50 p-4">
          <p className="text-sm font-medium text-red-700">期限超過</p>
          <p className="mt-1 text-3xl font-bold text-red-700">
            {tasks.overdue_count.toLocaleString("ja-JP")}
          </p>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <p className="text-sm font-medium text-gray-500">未完了タスク合計</p>
          <p className="mt-1 text-3xl font-bold text-gray-900">
            {tasks.total_count.toLocaleString("ja-JP")}
          </p>
        </div>
      </div>

      <section>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="data-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left">タスク名</th>
                <th className="text-left">ステータス</th>
                <th className="text-left">期限</th>
                <th className="text-left">担当者</th>
                <th className="text-left">ボール</th>
                <th className="text-left">案件紐付け</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tasks.tasks.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    未完了のタスクはありません
                  </td>
                </tr>
              ) : (
                tasks.tasks.map((task) => (
                  <tr key={task.notion_page_id} className={taskRowClassName(task)}>
                    <td>{task.title_summary}</td>
                    <td>{task.status ?? "-"}</td>
                    <td
                      className={
                        task.is_overdue
                          ? "px-4 py-2 font-semibold text-red-700"
                          : "px-4 py-2 text-gray-900"
                      }
                    >
                      {task.due_date ?? "未設定"}
                      {task.is_overdue && (
                        <span className="ml-2 rounded bg-red-200 px-1.5 py-0.5 text-xs font-semibold text-red-800">
                          期限超過
                        </span>
                      )}
                    </td>
                    <td>{joinOrDash(task.assignees)}</td>
                    <td>{joinOrDash(task.ball)}</td>
                    <td>
                      {task.has_project_link ? "あり" : "なし"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
