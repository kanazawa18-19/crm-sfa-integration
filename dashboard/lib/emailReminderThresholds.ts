// 未返信メールリマインド(2026-08-16)の選択可能な閾値一覧(時間単位、3〜72の3時間刻み)。
// `app/actions.ts`(Server Action、バリデーションに使用)と
// `app/(dashboard)/settings/email-reminders/page.tsx`(チェックボックスUI)の両方から使う。
// "use server"ファイル(actions.ts)は非同期関数以外をexportできない制約があるため、
// この定数はactions.tsから切り出して独立したモジュールに置く。
export const EMAIL_REMINDER_THRESHOLD_OPTIONS = Array.from({ length: 24 }, (_, i) => (i + 1) * 3);
