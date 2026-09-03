import { beforeEach, describe, expect, it, vi } from "vitest";

// 「送ってよい根拠」の登録・取り消しの検証(2026-09-03)。
//
// **画面から来た値をそのまま保存しないこと**が、このルートの一番大事な性質。
// 根拠はメールアドレスでも突き合わせるため、任意のアドレスに「送ってよい」を
// 登録できると、そのまま「送ってはいけない相手に送る」に化ける。

const findUnique = vi.fn();
const upsert = vi.fn();
const update = vi.fn();
const auditCreate = vi.fn();
const getCurrentUser = vi.fn();
const consentOverview = vi.fn();
const transaction = vi.fn(async (ops: unknown[]) => ops);

vi.mock("@/lib/prisma", () => ({
  default: {
    // $transaction に渡される「操作」は、実際にはPromiseではなくPrismaのクエリ記述。
    // ここでは引数をそのまま記録できれば十分なので、各メソッドは呼び出し引数を
    // 記録するだけのモックにしてある。
    $transaction: (ops: unknown[]) => transaction(ops),
    contactMailConsent: {
      findUnique: (...args: unknown[]) => findUnique(...args),
      upsert: (...args: unknown[]) => upsert(...args),
      update: (...args: unknown[]) => update(...args),
    },
    auditLog: { create: (...args: unknown[]) => auditCreate(...args) },
  },
}));
vi.mock("@/lib/auth", () => ({ getCurrentUser: () => getCurrentUser() }));
vi.mock("@/lib/backend", () => ({
  bulkEmailConsentOverview: (...args: unknown[]) => consentOverview(...args),
  BackendApiError: class extends Error {
    status?: number;
  },
  getErrorMessage: (error: unknown) => (error instanceof Error ? error.message : "不明"),
}));

const { POST, DELETE } = await import("@/app/api/bulk-email/consent/route");

const PAGE_ID = "3ced8ea8-1234-814a-83ce-cb3645539acd";
const NORMALIZED = "3ced8ea81234814a83cecb3645539acd";

function request(payload: unknown) {
  return { json: async () => payload } as never;
}

function body(overrides: Record<string, unknown> = {}) {
  return {
    client_page_id: "cli-1",
    contact_page_id: PAGE_ID,
    basis: "notified",
    obtained_at: "2026-04-08",
    evidence: "大阪ホテル展で名刺交換",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  getCurrentUser.mockResolvedValue({
    id: "u1",
    email: "kanazawa@cnctor.jp",
    role: "editor",
    name: "金沢",
  });
  consentOverview.mockResolvedValue({
    contacts: [
      {
        contact_page_id: PAGE_ID,
        contact_name: "山田太郎",
        client_name: "テスト商事",
        email: "yamada@example.com",
        unsubscribed: false,
        consent: {},
      },
    ],
  });
  findUnique.mockResolvedValue(null);
});

describe("根拠の登録", () => {
  it("Notionから取り直したアドレスで保存する", async () => {
    // 画面から偽のアドレスを送ってきても、保存されるのはNotion側の値。
    const response = await POST(request(body({ contact_email: "attacker@example.com" })));
    expect(response.status).toBe(200);

    const args = upsert.mock.calls[0][0];
    expect(args.where).toEqual({ contactPageId: NORMALIZED });
    expect(args.create.contactEmail).toBe("yamada@example.com");
    expect(args.create.recordedBy).toBe("kanazawa@cnctor.jp");
    expect(args.create.obtainedAt.toISOString()).toBe("2026-04-08T00:00:00.000Z");
  });

  it("登録し直すと取り消しが解除される", async () => {
    await POST(request(body()));
    expect(upsert.mock.calls[0][0].update).toMatchObject({ revokedAt: null, revokedBy: null });
  });

  it("誰がいつ判断したかを監査ログに残す", async () => {
    await POST(request(body()));
    const args = auditCreate.mock.calls[0][0].data;
    expect(args.actorSource).toBe("dashboard_bulk_email_consent");
    expect(args.actorLabel).toBe("kanazawa@cnctor.jp");
    expect(args.notionPageId).toBe(PAGE_ID);
  });

  it("その取引先にいない連絡先は登録できない", async () => {
    consentOverview.mockResolvedValue({ contacts: [] });
    const response = await POST(request(body()));
    expect(response.status).toBe(404);
    expect(upsert).not.toHaveBeenCalled();
  });

  it("知らない種類は受け付けない", async () => {
    const response = await POST(request(body({ basis: "むかしの種類" })));
    expect(response.status).toBe(400);
    expect(upsert).not.toHaveBeenCalled();
  });

  it("証跡が空なら受け付けない", async () => {
    // 後から誰も裏を取れない根拠を作らせない。
    const response = await POST(request(body({ evidence: "   " })));
    expect(response.status).toBe(400);
    expect(upsert).not.toHaveBeenCalled();
  });

  it("証跡が長すぎれば受け付けない", async () => {
    const response = await POST(request(body({ evidence: "あ".repeat(2001) })));
    expect(response.status).toBe(400);
    expect(upsert).not.toHaveBeenCalled();
  });

  it("未来の取得日は受け付けない", async () => {
    const response = await POST(request(body({ obtained_at: "2062-04-08" })));
    expect(response.status).toBe(400);
  });

  it("実在しない日付は受け付けない", async () => {
    expect((await POST(request(body({ obtained_at: "2026-02-31" })))).status).toBe(400);
  });

  it("閲覧者は登録できない", async () => {
    getCurrentUser.mockResolvedValue({ id: "u2", email: "v@example.com", role: "viewer" });
    const response = await POST(request(body()));
    expect(response.status).toBe(403);
    expect(upsert).not.toHaveBeenCalled();
  });

  it("ログインしていなければ401", async () => {
    getCurrentUser.mockResolvedValue(null);
    expect((await POST(request(body()))).status).toBe(401);
  });
});

describe("根拠の取り消し", () => {
  const del = () => request({ client_page_id: "cli-1", contact_page_id: PAGE_ID });

  it("行は消さずに取り消し日を入れる", async () => {
    findUnique.mockResolvedValue({
      basis: "notified",
      obtainedAt: new Date(),
      evidence: "名刺",
      contactEmail: "yamada@example.com",
      revokedAt: null,
    });
    const response = await DELETE(del());

    expect(response.status).toBe(200);
    const args = update.mock.calls[0][0];
    expect(args.where).toEqual({ contactPageId: NORMALIZED });
    expect(args.data.revokedAt).toBeInstanceOf(Date);
    expect(args.data.revokedBy).toBe("kanazawa@cnctor.jp");
  });

  it("★登録と同じ所属確認を通す（任意の連絡先を勝手に取り消せない）", async () => {
    consentOverview.mockResolvedValue({ contacts: [] });
    const response = await DELETE(del());
    expect(response.status).toBe(404);
    expect(update).not.toHaveBeenCalled();
  });

  it("取引先を指定しなければ受け付けない", async () => {
    const response = await DELETE(request({ contact_page_id: PAGE_ID }));
    expect(response.status).toBe(400);
    expect(update).not.toHaveBeenCalled();
  });

  it("2回押しても最初に取り消した時刻を書き換えない", async () => {
    findUnique.mockResolvedValue({
      basis: "notified",
      obtainedAt: new Date(),
      evidence: "名刺",
      contactEmail: "yamada@example.com",
      revokedAt: new Date("2026-08-01T00:00:00.000Z"),
    });
    const response = await DELETE(del());
    expect(response.status).toBe(200);
    expect(update).not.toHaveBeenCalled();
  });

  it("登録が無ければ404", async () => {
    findUnique.mockResolvedValue(null);
    expect((await DELETE(del())).status).toBe(404);
    expect(update).not.toHaveBeenCalled();
  });
});
