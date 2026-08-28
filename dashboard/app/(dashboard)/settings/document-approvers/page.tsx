import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import SubmitButton from "@/components/SubmitButton";
import { createDocumentApprover, deleteDocumentApprover, toggleDocumentApproverActive } from "./actions";

export const dynamic = "force-dynamic";

// 見積書の承認者候補(平本さん・黒井さん等)の管理画面(2026-08-18)。app/(dashboard)/users/
// page.tsxと同じパターン(管理者専用、Server Actionでフォーム送信)。
export default async function DocumentApproversPage() {
  await requireRole("master");
  const approvers = await prisma.documentApprover.findMany({ orderBy: { createdAt: "asc" } });

  return (
    <div>
      <h1 className="page-title">見積書承認者管理</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        見積書の承認リクエスト送信時に選択できる承認者の一覧です。無効化した承認者は
        書類作成画面の選択肢から外れます(過去の承認リクエスト自体には影響しません)。
      </p>

      <section className="surface-card mt-6 p-5">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">承認者を追加</h2>
        <form action={createDocumentApprover} className="mt-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
            氏名
            <input name="name" required className="input" placeholder="平本來輝" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
            メールアドレス
            <input name="email" type="email" required className="input" placeholder="hiramoto@cnctor.jp" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
            役職(任意)
            <input name="title" className="input" placeholder="マーケティング本部長" />
          </label>
          <SubmitButton pendingLabel="追加中..." className="btn-primary">
            追加
          </SubmitButton>
        </form>
      </section>

      <div className="surface-card mt-6 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr className="bg-(--color-surface-muted)/60">
                <th>氏名</th>
                <th>メールアドレス</th>
                <th>役職</th>
                <th>状態</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {approvers.length === 0 ? (
                <tr>
                  <td className="text-(--color-foreground)/60" colSpan={5}>
                    承認者が登録されていません。
                  </td>
                </tr>
              ) : (
                approvers.map((approver) => (
                  <tr key={approver.id}>
                    <td>{approver.name}</td>
                    <td>{approver.email}</td>
                    <td>{approver.title ?? "-"}</td>
                    <td>
                      {approver.active ? (
                        <span className="inline-flex items-center rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/40 dark:text-green-300">
                          有効
                        </span>
                      ) : (
                        <span className="badge-muted">無効</span>
                      )}
                    </td>
                    <td className="flex gap-3">
                      <form action={toggleDocumentApproverActive}>
                        <input type="hidden" name="id" value={approver.id} />
                        <SubmitButton
                          pendingLabel="変更中..."
                          className="link text-xs disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          {approver.active ? "無効化" : "有効化"}
                        </SubmitButton>
                      </form>
                      <form action={deleteDocumentApprover}>
                        <input type="hidden" name="id" value={approver.id} />
                        <SubmitButton
                          pendingLabel="削除中..."
                          className="text-xs text-(--brand-danger) underline disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          削除
                        </SubmitButton>
                      </form>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
