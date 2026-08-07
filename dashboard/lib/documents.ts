// 書類生成（見積書・申込書・契約書）のクライアント/サーバー共通ロジック。
// バックエンドの秘密情報は扱わないため、クライアントコンポーネントからも利用できる。

export const DOCUMENT_CATEGORIES = ["見積書", "申込書", "契約書"] as const;

export type DocumentCategory = (typeof DOCUMENT_CATEGORIES)[number];

// バックエンド（src/document_generation/common.py の BASELINE_NOTE）と文言を揃えること。
// 全カテゴリ共通の定型注記であり、宛先未反映・タブ複製先の確認等の個別注記とは
// 視覚的に区別して表示する（obasan-qualityレビュー: 両者が同列に見え重要な個別注記を
// 見落とすリスクがあるとの指摘を反映）。
export const BASELINE_NOTE =
  "自動生成された書類です。内容（宛先・件名・金額・印影等）を必ず確認してから送付してください。";

export const DOCUMENT_CATEGORY_EXTENSIONS: Record<DocumentCategory, string> = {
  見積書: "pdf",
  申込書: "xlsx",
  契約書: "docx",
};

/**
 * X-Document-Notesの配列を、全カテゴリ共通の定型注記（BASELINE_NOTE）と、
 * 個別の注記（宛先未反映・タブ複製先の確認等、案件・テンプレート固有の重要な情報）に分ける。
 */
export function splitDocumentNotes(notes: string[]): {
  baselineNote: string | null;
  specificNotes: string[];
} {
  const baselineNote = notes.find((note) => note === BASELINE_NOTE) ?? null;
  const specificNotes = notes.filter((note) => note !== BASELINE_NOTE);
  return { baselineNote, specificNotes };
}

/**
 * Content-Dispositionが取得できなかった場合のフォールバックファイル名を組み立てる。
 * 拡張子が無いとOS/ブラウザ側でファイル種別を認識できないため必ず付与する。
 */
export function buildFallbackFilename(projectName: string, category: DocumentCategory): string {
  return `${projectName}_${category}.${DOCUMENT_CATEGORY_EXTENSIONS[category]}`;
}

/**
 * Content-Disposition ヘッダー（RFC 5987 形式 `filename*=UTF-8''<url-encoded>` を想定）
 * からファイル名を抽出する。抽出できない場合は null を返す。
 */
export function parseFilenameFromContentDisposition(header: string | null): string | null {
  if (!header) {
    return null;
  }

  const extendedMatch = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (extendedMatch) {
    try {
      return decodeURIComponent(extendedMatch[1]);
    } catch {
      return null;
    }
  }

  const simpleMatch = header.match(/filename="?([^";]+)"?/i);
  if (simpleMatch) {
    return simpleMatch[1];
  }

  return null;
}

/**
 * X-Document-Notes ヘッダー（`json.dumps(notes, ensure_ascii=False)` をさらに
 * `urllib.parse.quote` した文字列）をデコードし、注意事項の配列に変換する。
 * 形式が不正な場合は空配列を返す。
 */
export function parseDocumentNotes(header: string | null): string[] {
  if (!header) {
    return [];
  }

  try {
    const decoded = decodeURIComponent(header);
    const parsed = JSON.parse(decoded);
    if (Array.isArray(parsed) && parsed.every((item) => typeof item === "string")) {
      return parsed;
    }
    return [];
  } catch {
    return [];
  }
}
