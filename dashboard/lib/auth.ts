import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import { COOKIE_NAME, verifySessionToken } from "@/lib/adminSession";

// web-engagement-toolのlib/auth.tsと同じ実装(2026-08-15移植)。単一の共有
// DASHBOARD_PASSWORDによるセッション方式(旧実装)を置き換え、ユーザーごとの
// アカウント・ロールベースの認証にする。

export type CurrentUser = {
  id: string;
  email: string;
  role: "master" | "editor" | "viewer";
  name: string | null;
  avatarUrl: string | null;
};

const ROLE_ORDER = { viewer: 0, editor: 1, master: 2 } as const;
const ROLE_LABELS_JA = { viewer: "閲覧者", editor: "編集者", master: "管理者" } as const;

export async function getCurrentUser(): Promise<CurrentUser | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  const verified = verifySessionToken(token);
  if (!verified) return null;

  const user = await prisma.user.findUnique({ where: { id: verified.userId } });
  if (!user) return null;

  return { id: user.id, email: user.email, role: user.role, name: user.name, avatarUrl: user.avatarUrl };
}

/** Server Component guard — redirects to login if signed out, or throws if the role isn't high enough. */
export async function requireRole(minRole: keyof typeof ROLE_ORDER): Promise<CurrentUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (ROLE_ORDER[user.role] < ROLE_ORDER[minRole]) {
    throw new Error(`この操作には${ROLE_LABELS_JA[minRole]}以上の権限が必要です`);
  }
  return user;
}
