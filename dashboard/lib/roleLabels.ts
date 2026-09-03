import type { CurrentUser } from "@/lib/auth";

// Sidebar.tsx / AvatarMenu.tsxで重複定義されていたロール表示ラベルを共通化
// (2026-08-17)。
export const ROLE_LABELS: Record<CurrentUser["role"], string> = {
  master: "管理者",
  editor: "編集者",
  viewer: "閲覧者",
};

// 権限の強さの順序(2026-09-03)。lib/auth.tsのROLE_ORDERと同じ値だが、auth.tsは
// next/headersを読むためClient Componentからimportできない。サイドバーのように
// クライアント側でも「この人にこのリンクを出してよいか」を判定したい箇所があるため、
// 値だけをこの共有モジュールにも置く。
//
// **画面の出し分けはここ、実際の遮断はサーバー側(requireRole)。**
// リンクを隠すだけでは権限制御にならない(URLを直接叩けば通る)ので、ページ側の
// requireRoleを省略しないこと。
export const ROLE_ORDER: Record<CurrentUser["role"], number> = {
  viewer: 0,
  editor: 1,
  master: 2,
};

