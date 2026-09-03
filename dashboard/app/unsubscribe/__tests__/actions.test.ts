import { beforeEach, describe, expect, it, vi } from "vitest";

// 配信停止の実行(Server Action)の検証(2026-09-03)。
//
// **法令対応の核心なので、prismaとredirectをモックしてでも通す。**
// ここが黙って効かなくなると、止めてほしいと言ったお客様に営業メールが届き続ける。

const upsert = vi.fn();
const findUnique = vi.fn();
const findFirst = vi.fn();
const redirect = vi.fn((path: string) => {
  // 本物のredirect()は例外を投げて以降の処理を止める。同じ形にしないと、
  // 「不正なリンクなのにDBへ書き込む」バグをテストが見逃す。
  throw new Error(`NEXT_REDIRECT:${path}`);
});

vi.mock("@/lib/prisma", () => ({
  default: {
    contactMailPreference: {
      upsert: (...args: unknown[]) => upsert(...args),
      findUnique: (...args: unknown[]) => findUnique(...args),
    },
    emailLog: { findFirst: (...args: unknown[]) => findFirst(...args) },
  },
}));
vi.mock("next/navigation", () => ({ redirect: (path: string) => redirect(path) }));

const { unsubscribeAction } = await import("@/app/unsubscribe/actions");
const { buildUnsubscribeToken } = await import("@/lib/bulkEmailUnsubscribe");

const SECRET = "test-secret";
const PAGE_ID = "3ced8ea8-1234-814a-83ce-cb3645539acd";
const NORMALIZED = "3ced8ea81234814a83cecb3645539acd";

function formData(overrides: Record<string, string> = {}): FormData {
  const data = new FormData();
  data.set("c", PAGE_ID);
  data.set("t", buildUnsubscribeToken(SECRET, PAGE_ID));
  for (const [key, value] of Object.entries(overrides)) data.set(key, value);
  return data;
}

async function run(data: FormData): Promise<string> {
  try {
    await unsubscribeAction(data);
  } catch (error) {
    return String((error as Error).message).replace("NEXT_REDIRECT:", "");
  }
  throw new Error("redirectされなかった");
}

describe("unsubscribeAction", () => {
  beforeEach(() => {
    process.env.BULK_EMAIL_UNSUBSCRIBE_SECRET = SECRET;
    upsert.mockReset().mockResolvedValue({});
    findUnique.mockReset().mockResolvedValue(null);
    findFirst.mockReset().mockResolvedValue({ contactEmail: "yamada@example.com" });
    redirect.mockClear();
  });

  it("正しいリンクなら配信停止を記録して完了画面へ送る", async () => {
    expect(await run(formData())).toBe("/unsubscribe/done");
    expect(upsert).toHaveBeenCalledTimes(1);
    const args = upsert.mock.calls[0][0];
    expect(args.where).toEqual({ contactPageId: NORMALIZED });
    expect(args.create).toMatchObject({ unsubscribed: true, contactEmail: "yamada@example.com" });
  });

  it("既に行がある場合でも unsubscribed を true に上げ直す", async () => {
    // 社内での手入力(source:"manual")で unsubscribed:false の行ができた将来を想定。
    // ここが抜けていると「完了しました」と出しながら止まっていない状態になる。
    await run(formData());
    expect(upsert.mock.calls[0][0].update).toMatchObject({ unsubscribed: true });
  });

  it("停止中の相手が2回押しても停止日時は動かさない（最初の申し出の時期を残す）", async () => {
    findUnique.mockResolvedValue({ unsubscribed: true });
    await run(formData());
    expect(upsert.mock.calls[0][0].update).not.toHaveProperty("unsubscribedAt");
  });

  it("一度解除された行を停止し直したときは停止日時を進める", async () => {
    findUnique.mockResolvedValue({ unsubscribed: false });
    await run(formData());
    expect(upsert.mock.calls[0][0].update.unsubscribedAt).toBeInstanceOf(Date);
  });

  it("正規化済みのページIDの形でなければDBに触らない", async () => {
    // DB側にも同じ形のCHECK制約がある。ここで止めないとお客様の画面がDBエラーになる。
    const data = new FormData();
    data.set("c", "みじかい");
    data.set("t", buildUnsubscribeToken(SECRET, "みじかい"));
    expect(await run(data)).toBe("/unsubscribe/done?status=invalid");
    expect(upsert).not.toHaveBeenCalled();
  });

  it("EmailLogは正規化前後の両方のページIDで探す", async () => {
    await run(formData());
    expect(findFirst.mock.calls[0][0].where.contactPageId.in).toEqual([NORMALIZED, PAGE_ID]);
  });

  it("やり取りの記録が無ければメールアドレスは空で作る（page_idでの除外は効く）", async () => {
    findFirst.mockResolvedValue(null);
    await run(formData());
    expect(upsert.mock.calls[0][0].create.contactEmail).toBe("");
  });

  it("署名が違えばDBに触らない", async () => {
    expect(await run(formData({ t: "でたらめ" }))).toBe("/unsubscribe/done?status=invalid");
    expect(upsert).not.toHaveBeenCalled();
  });

  it("署名が空でもDBに触らない", async () => {
    expect(await run(formData({ t: "" }))).toBe("/unsubscribe/done?status=invalid");
    expect(upsert).not.toHaveBeenCalled();
  });

  it("ページIDが空ならDBに触らない", async () => {
    expect(await run(formData({ c: "" }))).toBe("/unsubscribe/done?status=invalid");
    expect(upsert).not.toHaveBeenCalled();
  });

  it("鍵が未設定なら誰も停止できない（勝手に通さない）", async () => {
    delete process.env.BULK_EMAIL_UNSUBSCRIBE_SECRET;
    expect(await run(formData())).toBe("/unsubscribe/done?status=invalid");
    expect(upsert).not.toHaveBeenCalled();
  });
});
