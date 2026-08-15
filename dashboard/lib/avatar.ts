// アイコン画像アップロード(/settings/profile, 2026-08-16)のバリデーション。実際の
// アップロード処理(Vercel Blob)はapp/actions.tsのupdateOwnAvatar側に置き、ここでは
// Server Action外からも(テストからも)呼べる純粋関数だけを置く。

export const AVATAR_MAX_BYTES = 2 * 1024 * 1024; // 2MB
export const AVATAR_ALLOWED_TYPES: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
};

/** 妥当なら null、不正ならエラーメッセージ(日本語、そのままUIに表示できる文言)を返す。 */
export function validateAvatarFile(file: { type: string; size: number }): string | null {
  if (file.size === 0) {
    return "画像ファイルを選択してください";
  }
  if (!(file.type in AVATAR_ALLOWED_TYPES)) {
    return "png / jpeg / webp形式の画像を選択してください";
  }
  if (file.size > AVATAR_MAX_BYTES) {
    return "画像サイズは2MB以内にしてください";
  }
  return null;
}
