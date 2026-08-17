import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { changeUserRole, deleteUser } from "@/app/actions";
import SubmitButton from "@/components/SubmitButton";
import InviteUserForm from "./InviteUserForm";

export const dynamic = "force-dynamic";

const ROLE_LABELS: Record<string, string> = {
  viewer: "閲覧者",
  editor: "編集者",
  master: "管理者",
};

export default async function UsersPage() {
  const currentUser = await requireRole("master");
  const users = await prisma.user.findMany({ orderBy: { createdAt: "asc" } });

  return (
    <div>
      <h1 className="page-title">ユーザー管理</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        管理者は全操作、編集者は追加・編集(危険な操作や設定変更は不可)、閲覧者は閲覧のみ可能です。
      </p>

      <section className="surface-card mt-6 p-5">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">ユーザーを招待</h2>
        <p className="mt-1 text-xs text-(--color-foreground)/50">
          招待メールが送信され、本人がパスワードを設定して初回ログインします(SMTP未設定の場合はサーバーログにリンクが出力されます)。
        </p>
        <InviteUserForm />
      </section>

      <div className="surface-card mt-6 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                <th className="px-4 py-2 font-medium">メールアドレス</th>
                <th className="font-medium">権限</th>
                <th className="font-medium">状態</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-(--border-subtle) last:border-0">
                  <td className="px-4 py-2">
                    {u.email}
                    {u.id === currentUser.id && <span className="badge-blue ml-2">あなた</span>}
                  </td>
                  <td>
                    <form action={changeUserRole} className="flex items-center gap-2">
                      <input type="hidden" name="id" value={u.id} />
                      <select name="role" defaultValue={u.role} className="input py-1 text-xs">
                        {Object.entries(ROLE_LABELS).map(([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ))}
                      </select>
                      <SubmitButton pendingLabel="変更中..." className="link text-xs disabled:cursor-not-allowed disabled:opacity-40">
                        変更
                      </SubmitButton>
                    </form>
                  </td>
                  <td>
                    {u.passwordHash ? (
                      <span className="inline-flex items-center rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/40 dark:text-green-300">
                        有効
                      </span>
                    ) : (
                      <span className="badge-muted">招待中</span>
                    )}
                  </td>
                  <td className="px-4">
                    {/* 有効なmasterアカウントは削除不可。招待中(passwordHash未設定)のmaster招待は
                        キャンセルできる — app/actions.tsのdeleteUser()と同じ条件。 */}
                    {u.id !== currentUser.id && (u.role !== "master" || !u.passwordHash) && (
                      <form action={deleteUser}>
                        <input type="hidden" name="id" value={u.id} />
                        <SubmitButton
                          pendingLabel={u.passwordHash ? "削除中..." : "キャンセル中..."}
                          className="text-xs text-(--brand-danger) underline disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          {u.passwordHash ? "削除" : "招待をキャンセル"}
                        </SubmitButton>
                      </form>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
