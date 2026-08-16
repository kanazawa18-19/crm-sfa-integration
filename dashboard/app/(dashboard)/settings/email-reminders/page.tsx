import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { updateEmailReminderSettings } from "@/app/actions";
import { EMAIL_REMINDER_THRESHOLD_OPTIONS } from "@/lib/emailReminderThresholds";

export const dynamic = "force-dynamic";

export default async function EmailReminderSettingsPage() {
  await requireRole("master");
  const settings = await prisma.appSettings.findUnique({ where: { id: 1 } });
  const enabledThresholds = new Set(settings?.emailReminderThresholdHours ?? []);

  return (
    <div>
      <h1 className="page-title">未返信メールリマインド設定</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        連絡先からの受信メールに未返信のまま経過した場合、担当営業へSlack DMでリマインドします。デフォルトはOFFです。
      </p>

      <form action={updateEmailReminderSettings} className="mt-6 flex flex-col gap-6">
        <section className="surface-card p-5">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              name="emailReminderEnabled"
              defaultChecked={settings?.emailReminderEnabled ?? false}
              className="h-4 w-4"
            />
            <span className="text-sm font-semibold text-(--color-foreground)/80">
              未返信メールリマインドを有効にする
            </span>
          </label>
          <p className="mt-1 text-xs text-(--color-foreground)/50">
            ONにすると、以下で選択した経過時間ごとに、未返信のメールがある担当営業へSlack DMでリマインドが届きます。
          </p>
        </section>

        <section className="surface-card p-5">
          <span className="text-sm font-semibold text-(--color-foreground)/80">リマインドのタイミング</span>
          <p className="mt-1 text-xs text-(--color-foreground)/50">
            受信メールから何時間経過した時点でリマインドするかを、3時間刻み(3〜72時間)から選択式で複数指定できます。
          </p>
          <div className="mt-3 grid grid-cols-4 gap-2 sm:grid-cols-6">
            {EMAIL_REMINDER_THRESHOLD_OPTIONS.map((hours) => (
              <label key={hours} className="flex items-center gap-1.5 text-sm text-(--color-foreground)/80">
                <input
                  type="checkbox"
                  name="emailReminderThresholdHours"
                  value={hours}
                  defaultChecked={enabledThresholds.has(hours)}
                  className="h-4 w-4"
                />
                {hours}時間
              </label>
            ))}
          </div>
        </section>

        <button type="submit" className="btn-primary w-fit">
          保存する
        </button>
      </form>
    </div>
  );
}
