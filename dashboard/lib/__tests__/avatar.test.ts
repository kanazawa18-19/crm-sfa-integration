import { describe, expect, it } from "vitest";
import { AVATAR_MAX_BYTES, validateAvatarFile } from "@/lib/avatar";

describe("validateAvatarFile", () => {
  it("png/jpeg/webpで2MB以内ならnull(妥当)を返す", () => {
    expect(validateAvatarFile({ type: "image/png", size: 1024 })).toBeNull();
    expect(validateAvatarFile({ type: "image/jpeg", size: 1024 })).toBeNull();
    expect(validateAvatarFile({ type: "image/webp", size: AVATAR_MAX_BYTES })).toBeNull();
  });

  it("サイズ0(未選択相当)はエラーになる", () => {
    expect(validateAvatarFile({ type: "image/png", size: 0 })).toBe("画像ファイルを選択してください");
  });

  it("許可されていない拡張子(例: image/gif, application/pdf)はエラーになる", () => {
    expect(validateAvatarFile({ type: "image/gif", size: 1024 })).toBe(
      "png / jpeg / webp形式の画像を選択してください"
    );
    expect(validateAvatarFile({ type: "application/pdf", size: 1024 })).toBe(
      "png / jpeg / webp形式の画像を選択してください"
    );
  });

  it("2MBを超えるとエラーになる", () => {
    expect(validateAvatarFile({ type: "image/png", size: AVATAR_MAX_BYTES + 1 })).toBe(
      "画像サイズは2MB以内にしてください"
    );
  });
});
