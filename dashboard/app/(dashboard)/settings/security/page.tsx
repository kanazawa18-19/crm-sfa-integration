import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { updateSecuritySettings } from "@/app/actions";
import { ALWAYS_ALLOWED_IPS } from "@/lib/ipAllowlist";
import SubmitButton from "@/components/SubmitButton";

export const dynamic = "force-dynamic";

export default async function SecuritySettingsPage() {
  await requireRole("master");
  const settings = await prisma.appSettings.findUnique({ where: { id: 1 } });

  return (
    <div>
      <h1 className="page-title">セキュリティ設定</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        2要素認証とIPアドレス制限を設定します。いずれもデフォルトはOFFです。
      </p>

      <form action={updateSecuritySettings} className="mt-6 flex flex-col gap-6">
        <section className="surface-card p-5">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              name="twoFactorEnabled"
              defaultChecked={settings?.twoFactorEnabled ?? false}
              className="h-4 w-4"
            />
            <span className="text-sm font-semibold text-(--color-foreground)/80">2要素認証を必須にする</span>
          </label>
          <p className="mt-1 text-xs text-(--color-foreground)/50">
            ONにすると、全ユーザーが次回ログイン時に認証アプリまたはメールでの2要素認証設定を求められます。
          </p>
        </section>

        <section className="surface-card p-5">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              name="ipAllowlistEnabled"
              defaultChecked={settings?.ipAllowlistEnabled ?? false}
              className="h-4 w-4"
            />
            <span className="text-sm font-semibold text-(--color-foreground)/80">
              許可IPアドレス以外からのアクセスを拒否する
            </span>
          </label>
          <p className="mt-1 text-xs text-(--color-foreground)/50">
            以下の常時許可拠点に加え、下の欄に入力したIPアドレス/CIDR(例: 203.0.113.5、203.0.113.0/24)からのみアクセスを許可します。1行に1件。
          </p>
          <ul className="mt-2 text-xs text-(--color-foreground)/50">
            {ALWAYS_ALLOWED_IPS.map((entry) => (
              <li key={entry.ip}>
                {entry.label}: {entry.ip}（常時許可・設定不可）
              </li>
            ))}
          </ul>
          <textarea
            name="ipAllowlist"
            rows={5}
            defaultValue={settings?.ipAllowlist.join("\n") ?? ""}
            placeholder={"203.0.113.5\n203.0.113.0/24"}
            className="input mt-2 w-full font-mono"
          />
        </section>

        <SubmitButton pendingLabel="保存中..." className="btn-primary w-fit">
          保存する
        </SubmitButton>
      </form>
    </div>
  );
}
