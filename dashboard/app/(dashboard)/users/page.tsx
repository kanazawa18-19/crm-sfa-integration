import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { changeUserRole, deleteUser, toggleUserIsManager } from "@/app/actions";
import SubmitButton from "@/components/SubmitButton";
import SwitchButton from "@/components/SwitchButton";
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
  const managerCount = users.filter((u) => u.isManager).length;

  return (
    <div>
      <h1 className="page-title">ユーザー管理</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        管理者は全操作、編集者は追加・編集(危険な操作や設定変更は不可)、閲覧者は閲覧のみ可能です。
      </p>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        「インシデント通知先」は上記のアクセス権限(権限列)とは別軸の設定です。ONにしたユーザーは、システムが検知した重大インシデント(スコア8点以上)や、kintone/Zoho連携での新規レコード登録の異常を検知した際に、Slack
        DMで通知を受け取ります。
      </p>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        現在の通知対象:{" "}
        <strong className={managerCount === 0 ? "text-(--brand-danger)" : undefined}>{managerCount}人</strong>
        {managerCount === 0 && "です。このままでは重大インシデントの通知が誰にも届きません。"}
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
          <table className="data-table">
            <thead>
              <tr className="bg-(--color-surface-muted)/60">
                <th>メールアドレス</th>
                <th>権限</th>
                <th>状態</th>
                <th className="border-l border-(--border-subtle)">インシデント通知先</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>
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
                      <span className="badge-green">有効</span>
                    ) : (
                      <span className="badge-muted">招待中</span>
                    )}
                  </td>
                  <td className="border-l border-(--border-subtle)">
                    {/* 通知対象が0人にならないよう、最後の1人はOFFにできない
                        (deleteUser()の「有効なmasterアカウントは削除不可」と同じ考え方
                        でフォーム自体を出さない。actions.tsのtoggleUserIsManager()にも
                        同じガードあり — obasan-qualityレビュー指摘、2026-08-25)。 */}
                    {u.isManager && managerCount <= 1 ? (
                      <span className="flex items-center gap-2">
                        <SwitchButton
                          checked
                          disabled
                          label={`${u.email} のインシデント通知先`}
                          pendingLabel="変更中..."
                          describedById={`last-manager-hint-${u.id}`}
                        />
                        <span id={`last-manager-hint-${u.id}`} className="text-xs text-(--text-grey)">
                          (最後の1人)
                        </span>
                      </span>
                    ) : (
                      <form action={toggleUserIsManager}>
                        <input type="hidden" name="id" value={u.id} />
                        <SwitchButton checked={u.isManager} label={`${u.email} のインシデント通知先`} pendingLabel="変更中..." />
                      </form>
                    )}
                  </td>
                  <td>
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
