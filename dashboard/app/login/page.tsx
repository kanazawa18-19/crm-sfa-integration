import LoginForm from "./LoginForm";

export const dynamic = "force-dynamic";

// Googleログインの失敗はコールバックからのリダイレクト(`?error=...`)で返るため、
// サーバー側でクエリを読んでフォームへ渡す。クライアント側でuseSearchParams()を
// 使うとプリレンダリングがSuspense境界を要求してビルドが落ちる(2026-08-31)。
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string | string[] }>;
}) {
  const { error } = await searchParams;
  const initialError = Array.isArray(error) ? error[0] : error;
  return <LoginForm initialError={initialError} />;
}
