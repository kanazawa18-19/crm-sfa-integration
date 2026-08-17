import type { CurrentUser } from "@/lib/auth";

// Sidebar.tsx / AvatarMenu.tsxで重複定義されていたロール表示ラベルを共通化
// (2026-08-17)。
export const ROLE_LABELS: Record<CurrentUser["role"], string> = {
  master: "管理者",
  editor: "編集者",
  viewer: "閲覧者",
};
