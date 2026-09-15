import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import NameForm from "./NameForm";
import AvatarForm from "./AvatarForm";

export const dynamic = "force-dynamic";

export default async function ProfilePage({
  searchParams,
}: {
  searchParams: Promise<{ emailChanged?: string }>;
}) {
  const currentUser = await requireRole("viewer");
  const { emailChanged } = await searchParams;

  const user = await prisma.user.findUnique({ where: { id: currentUser.id } });
  if (!user) {
    // requireRole()がDBを引いた直後にユーザーが削除されるような極端な競合以外は
    // 起こらないが、念のためnullを許容せずここで打ち切る。
    throw new Error("ユーザー情報の取得に失敗しました");
  }

  return (
    <div className="max-w-xl space-y-6">
      <h1 className="page-title">プロフィール編集</h1>
      <p className="text-(--text-grey)">自分の表示名・アイコン画像を編集できます。</p>

      {emailChanged === "1" && <div className="alert-success">メールアドレスを変更しました。</div>}

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">アイコン画像</h2>
        <div className="mt-3">
          <AvatarForm initialAvatarUrl={user.avatarUrl} />
        </div>
      </section>

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">表示名・役職・部署</h2>
        <div className="mt-3">
          <NameForm initialName={user.name ?? ""} initialTitle={user.title ?? ""} initialDepartment={user.department ?? ""} />
        </div>
      </section>

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">メールアドレス</h2>
        <p className="mt-1 text-xs text-(--color-foreground)/50">
          現在のメールアドレス: <span className="font-medium text-(--color-foreground)/80">{user.email}</span>
        </p>
        <p className="mt-1 text-xs text-(--color-foreground)/50">
          ログインは cnctor.jp の Google アカウントで行うため、メールアドレスとパスワードはここでは変更できません(2026-09-16〜)。
          アドレスを変えたいときは、管理者に新しいアドレスで招待し直してもらってください。
        </p>
      </section>
    </div>
  );
}
