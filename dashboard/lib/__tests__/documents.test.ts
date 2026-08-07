import { describe, expect, it } from "vitest";
import {
  BASELINE_NOTE,
  buildFallbackFilename,
  parseDocumentNotes,
  parseFilenameFromContentDisposition,
  splitDocumentNotes,
} from "@/lib/documents";

describe("parseFilenameFromContentDisposition", () => {
  it("RFC 5987 形式（filename*=UTF-8''...）からファイル名を抽出できる", () => {
    const header = "attachment; filename*=UTF-8''%E8%A6%8B%E7%A9%8D%E6%9B%B8.pdf";
    expect(parseFilenameFromContentDisposition(header)).toBe("見積書.pdf");
  });

  it("単純な filename= 形式からもファイル名を抽出できる", () => {
    expect(parseFilenameFromContentDisposition('attachment; filename="quote.pdf"')).toBe(
      "quote.pdf"
    );
  });

  it("ヘッダーが null の場合は null を返す", () => {
    expect(parseFilenameFromContentDisposition(null)).toBeNull();
  });

  it("ファイル名を含まないヘッダーの場合は null を返す", () => {
    expect(parseFilenameFromContentDisposition("attachment")).toBeNull();
  });
});

describe("parseDocumentNotes", () => {
  it("URLエンコードされたJSON配列をデコードできる", () => {
    const notes = ["テンプレートの『福住旅館』タブを複製して使用しました。"];
    const header = encodeURIComponent(JSON.stringify(notes));
    expect(parseDocumentNotes(header)).toEqual(notes);
  });

  it("空配列も扱える", () => {
    const header = encodeURIComponent(JSON.stringify([]));
    expect(parseDocumentNotes(header)).toEqual([]);
  });

  it("ヘッダーが null の場合は空配列を返す", () => {
    expect(parseDocumentNotes(null)).toEqual([]);
  });

  it("不正なJSONの場合は空配列を返す", () => {
    expect(parseDocumentNotes("not-json")).toEqual([]);
  });

  it("文字列配列でない場合は空配列を返す", () => {
    const header = encodeURIComponent(JSON.stringify([1, 2, 3]));
    expect(parseDocumentNotes(header)).toEqual([]);
  });
});

describe("splitDocumentNotes", () => {
  it("BASELINE_NOTEと個別注記を分離する", () => {
    const specific = "テンプレートの『福住旅館』タブを複製して使用しました。";
    const result = splitDocumentNotes([BASELINE_NOTE, specific]);

    expect(result.baselineNote).toBe(BASELINE_NOTE);
    expect(result.specificNotes).toEqual([specific]);
  });

  it("BASELINE_NOTEが含まれない場合はnullを返す", () => {
    const specific = "取引先名が案件データから取得できなかったため、宛先の差し込みは未反映です。";
    const result = splitDocumentNotes([specific]);

    expect(result.baselineNote).toBeNull();
    expect(result.specificNotes).toEqual([specific]);
  });

  it("空配列の場合は両方とも空になる", () => {
    const result = splitDocumentNotes([]);

    expect(result.baselineNote).toBeNull();
    expect(result.specificNotes).toEqual([]);
  });
});

describe("buildFallbackFilename", () => {
  it("カテゴリごとに正しい拡張子を付与する", () => {
    expect(buildFallbackFilename("サンプルホテル", "見積書")).toBe("サンプルホテル_見積書.pdf");
    expect(buildFallbackFilename("サンプルホテル", "申込書")).toBe("サンプルホテル_申込書.xlsx");
    expect(buildFallbackFilename("サンプルホテル", "契約書")).toBe("サンプルホテル_契約書.docx");
  });
});
