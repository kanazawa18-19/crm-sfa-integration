import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import NameForm from "./NameForm";
import EmailForm from "./EmailForm";
import PasswordForm from "./PasswordForm";
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
      <p className="text-(--text-grey)">自分の表示名・メールアドレス・パスワード・アイコン画像を編集できます。</p>

      {emailChanged === "1" && <div className="alert-success">メールアドレスを変更しました。</div>}

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">アイコン画像</h2>
        <div className="mt-3">
          <AvatarForm initialAvatarUrl={user.avatarUrl} />
        </div>
      </section>

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">表示名</h2>
        <div className="mt-3">
          <NameForm initialName={user.name ?? ""} />
        </div>
      </section>

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">メールアドレス</h2>
        <div className="mt-3">
          <EmailForm currentEmail={user.email} />
        </div>
      </section>

      <section className="surface-card p-6">
        <h2 className="text-sm font-semibold text-(--color-foreground)/70">パスワード</h2>
        <div className="mt-3">
          <PasswordForm />
        </div>
      </section>
    </div>
  );
}
