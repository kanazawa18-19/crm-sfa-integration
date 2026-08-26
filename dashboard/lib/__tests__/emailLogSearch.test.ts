import { describe, expect, it } from "vitest";
import { parseEmailLogQuery, EMAIL_LOG_SEARCH_HELP } from "@/lib/emailLogSearch";

// 固定した基準時刻(older_than:/newer_than:の相対日付計算をテストで決定的にするため)。
const NOW = new Date("2026-08-26T12:00:00+09:00");

describe("parseEmailLogQuery", () => {
  it("空文字列はフィルタなし(where=null)を返す", () => {
    const result = parseEmailLogQuery("", NOW);
    expect(result.where).toBeNull();
    expect(result.ignoredTerms).toEqual([]);
  });

  it("空白のみもフィルタなしを返す", () => {
    const result = parseEmailLogQuery("   ", NOW);
    expect(result.where).toBeNull();
  });

  it("素の単語は件名・スニペット・連絡先/担当者メールアドレスの横断検索になる", () => {
    const result = parseEmailLogQuery("見積もり", NOW);
    expect(result.where).toEqual({
      OR: [
        { subject: { contains: "見積もり", mode: "insensitive" } },
        { snippet: { contains: "見積もり", mode: "insensitive" } },
        { contactEmail: { contains: "見積もり", mode: "insensitive" } },
        { repEmail: { contains: "見積もり", mode: "insensitive" } },
      ],
    });
    expect(result.ignoredTerms).toEqual([]);
  });

  it("複数の素の単語は暗黙のANDになる", () => {
    const result = parseEmailLogQuery("見積もり 送付", NOW);
    expect(result.where).toEqual({
      AND: [
        {
          OR: [
            { subject: { contains: "見積もり", mode: "insensitive" } },
            { snippet: { contains: "見積もり", mode: "insensitive" } },
            { contactEmail: { contains: "見積もり", mode: "insensitive" } },
            { repEmail: { contains: "見積もり", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { subject: { contains: "送付", mode: "insensitive" } },
            { snippet: { contains: "送付", mode: "insensitive" } },
            { contactEmail: { contains: "送付", mode: "insensitive" } },
            { repEmail: { contains: "送付", mode: "insensitive" } },
          ],
        },
      ],
    });
  });

  it("from:はdirectionに応じてcontactEmail/repEmailを振り分ける", () => {
    const result = parseEmailLogQuery("from:example.com", NOW);
    expect(result.where).toEqual({
      OR: [
        { direction: "inbound", contactEmail: { contains: "example.com", mode: "insensitive" } },
        { direction: "outbound", repEmail: { contains: "example.com", mode: "insensitive" } },
      ],
    });
  });

  it("to:はfrom:と逆向きに振り分ける", () => {
    const result = parseEmailLogQuery("to:example.com", NOW);
    expect(result.where).toEqual({
      OR: [
        { direction: "inbound", repEmail: { contains: "example.com", mode: "insensitive" } },
        { direction: "outbound", contactEmail: { contains: "example.com", mode: "insensitive" } },
      ],
    });
  });

  it("subject:は件名のcontainsになる", () => {
    const result = parseEmailLogQuery("subject:御見積", NOW);
    expect(result.where).toEqual({ subject: { contains: "御見積", mode: "insensitive" } });
  });

  it('subject:"..."は引用符を含めずcontainsになる', () => {
    const result = parseEmailLogQuery('subject:"dinner movie"', NOW);
    expect(result.where).toEqual({ subject: { contains: "dinner movie", mode: "insensitive" } });
  });

  it("完全一致のクォート文字列は自由語(text)として扱われる", () => {
    const result = parseEmailLogQuery('"dinner and movie"', NOW);
    expect(result.where).toEqual({
      OR: [
        { subject: { contains: "dinner and movie", mode: "insensitive" } },
        { snippet: { contains: "dinner and movie", mode: "insensitive" } },
        { contactEmail: { contains: "dinner and movie", mode: "insensitive" } },
        { repEmail: { contains: "dinner and movie", mode: "insensitive" } },
      ],
    });
  });

  it("after:/before:は日付(JST 0時)のsentAtフィルタになる", () => {
    const result = parseEmailLogQuery("after:2026/08/01 before:2026/09/01", NOW);
    expect(result.where).toEqual({
      AND: [
        { sentAt: { gte: new Date("2026-08-01T00:00:00+09:00") } },
        { sentAt: { lt: new Date("2026-09-01T00:00:00+09:00") } },
      ],
    });
  });

  it("before:はハイフン区切りの日付も受け付ける", () => {
    const result = parseEmailLogQuery("before:2026-09-01", NOW);
    expect(result.where).toEqual({ sentAt: { lt: new Date("2026-09-01T00:00:00+09:00") } });
  });

  it("older_than:/newer_than:はNOWからの相対日付になる", () => {
    const result = parseEmailLogQuery("newer_than:7d", NOW);
    const expectedCutoff = new Date(NOW.getTime());
    expectedCutoff.setUTCDate(expectedCutoff.getUTCDate() - 7);
    expect(result.where).toEqual({ sentAt: { gte: expectedCutoff } });
  });

  it("older_than:1yは年単位で計算する", () => {
    const result = parseEmailLogQuery("older_than:1y", NOW);
    const expectedCutoff = new Date(NOW.getTime());
    expectedCutoff.setUTCFullYear(expectedCutoff.getUTCFullYear() - 1);
    expect(result.where).toEqual({ sentAt: { lt: expectedCutoff } });
  });

  it("OR演算子で複数条件をORにする", () => {
    const result = parseEmailLogQuery("from:amy OR from:david", NOW);
    expect(result.where).toEqual({
      OR: [
        {
          OR: [
            { direction: "inbound", contactEmail: { contains: "amy", mode: "insensitive" } },
            { direction: "outbound", repEmail: { contains: "amy", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { direction: "inbound", contactEmail: { contains: "david", mode: "insensitive" } },
            { direction: "outbound", repEmail: { contains: "david", mode: "insensitive" } },
          ],
        },
      ],
    });
  });

  it("小文字のorは演算子として扱われず自由語として検索される", () => {
    const result = parseEmailLogQuery("apple or banana", NOW);
    expect(result.where).toEqual({
      AND: [
        {
          OR: [
            { subject: { contains: "apple", mode: "insensitive" } },
            { snippet: { contains: "apple", mode: "insensitive" } },
            { contactEmail: { contains: "apple", mode: "insensitive" } },
            { repEmail: { contains: "apple", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { subject: { contains: "or", mode: "insensitive" } },
            { snippet: { contains: "or", mode: "insensitive" } },
            { contactEmail: { contains: "or", mode: "insensitive" } },
            { repEmail: { contains: "or", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { subject: { contains: "banana", mode: "insensitive" } },
            { snippet: { contains: "banana", mode: "insensitive" } },
            { contactEmail: { contains: "banana", mode: "insensitive" } },
            { repEmail: { contains: "banana", mode: "insensitive" } },
          ],
        },
      ],
    });
  });

  it("明示的なANDは暗黙のANDと同じ扱いになる", () => {
    const withAnd = parseEmailLogQuery("from:amy AND to:david", NOW);
    const withoutAnd = parseEmailLogQuery("from:amy to:david", NOW);
    expect(withAnd.where).toEqual(withoutAnd.where);
  });

  it("-による除外はNOTになる", () => {
    const result = parseEmailLogQuery("dinner -movie", NOW);
    expect(result.where).toEqual({
      AND: [
        {
          OR: [
            { subject: { contains: "dinner", mode: "insensitive" } },
            { snippet: { contains: "dinner", mode: "insensitive" } },
            { contactEmail: { contains: "dinner", mode: "insensitive" } },
            { repEmail: { contains: "dinner", mode: "insensitive" } },
          ],
        },
        {
          NOT: {
            OR: [
              { subject: { contains: "movie", mode: "insensitive" } },
              { snippet: { contains: "movie", mode: "insensitive" } },
              { contactEmail: { contains: "movie", mode: "insensitive" } },
              { repEmail: { contains: "movie", mode: "insensitive" } },
            ],
          },
        },
      ],
    });
  });

  it("-はフィールド指定にも適用できる", () => {
    const result = parseEmailLogQuery("-subject:テスト", NOW);
    expect(result.where).toEqual({ NOT: { subject: { contains: "テスト", mode: "insensitive" } } });
  });

  it('-は引用符付きフレーズにも適用できる(-"...")', () => {
    const result = parseEmailLogQuery('-"exact phrase"', NOW);
    expect(result.where).toEqual({
      NOT: {
        OR: [
          { subject: { contains: "exact phrase", mode: "insensitive" } },
          { snippet: { contains: "exact phrase", mode: "insensitive" } },
          { contactEmail: { contains: "exact phrase", mode: "insensitive" } },
          { repEmail: { contains: "exact phrase", mode: "insensitive" } },
        ],
      },
    });
  });

  it("-の直後に空白があると否定にならず単なる文字として扱われる", () => {
    const result = parseEmailLogQuery("- movie", NOW);
    // "-"単体と"movie"、2つの自由語ANDになる
    expect(result.where).toEqual({
      AND: [
        {
          OR: [
            { subject: { contains: "-", mode: "insensitive" } },
            { snippet: { contains: "-", mode: "insensitive" } },
            { contactEmail: { contains: "-", mode: "insensitive" } },
            { repEmail: { contains: "-", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { subject: { contains: "movie", mode: "insensitive" } },
            { snippet: { contains: "movie", mode: "insensitive" } },
            { contactEmail: { contains: "movie", mode: "insensitive" } },
            { repEmail: { contains: "movie", mode: "insensitive" } },
          ],
        },
      ],
    });
  });

  it("()によるグループ化が優先される", () => {
    const grouped = parseEmailLogQuery("(from:amy OR from:david) subject:見積", NOW);
    const fromLeaf = (name: string) => ({
      OR: [
        { direction: "inbound", contactEmail: { contains: name, mode: "insensitive" } },
        { direction: "outbound", repEmail: { contains: name, mode: "insensitive" } },
      ],
    });
    expect(grouped.where).toEqual({
      AND: [
        { OR: [fromLeaf("amy"), fromLeaf("david")] },
        { subject: { contains: "見積", mode: "insensitive" } },
      ],
    });
  });

  it("{}内はスペースがOR扱いになる", () => {
    const result = parseEmailLogQuery("{from:amy from:david}", NOW);
    const fromLeaf = (name: string) => ({
      OR: [
        { direction: "inbound", contactEmail: { contains: name, mode: "insensitive" } },
        { direction: "outbound", repEmail: { contains: name, mode: "insensitive" } },
      ],
    });
    expect(result.where).toEqual({ OR: [fromLeaf("amy"), fromLeaf("david")] });
  });

  it("has:attachment等の非対応フィールドはignoredTermsに(reason=unsupportedで)積まれ、フィルタからは無視される", () => {
    const result = parseEmailLogQuery("has:attachment subject:見積", NOW);
    expect(result.ignoredTerms).toEqual([{ raw: "has:attachment", reason: "unsupported" }]);
    expect(result.where).toEqual({ subject: { contains: "見積", mode: "insensitive" } });
  });

  it("label:/is:/cc:/bcc:等の非対応フィールドも同様にignoredTermsへ積まれる", () => {
    const result = parseEmailLogQuery("label:friends is:unread cc:x@example.com bcc:y@example.com", NOW);
    expect(result.ignoredTerms).toEqual([
      { raw: "label:friends", reason: "unsupported" },
      { raw: "is:unread", reason: "unsupported" },
      { raw: "cc:x@example.com", reason: "unsupported" },
      { raw: "bcc:y@example.com", reason: "unsupported" },
    ]);
    expect(result.where).toBeNull();
  });

  it("AROUND演算子は非対応としてignoredTermsに積まれる(前後の単語は自由語として残る)", () => {
    const result = parseEmailLogQuery("holiday AROUND 10 vacation", NOW);
    expect(result.ignoredTerms).toEqual([{ raw: "AROUND", reason: "unsupported" }]);
    expect(result.where).toEqual({
      AND: [
        {
          OR: [
            { subject: { contains: "holiday", mode: "insensitive" } },
            { snippet: { contains: "holiday", mode: "insensitive" } },
            { contactEmail: { contains: "holiday", mode: "insensitive" } },
            { repEmail: { contains: "holiday", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { subject: { contains: "10", mode: "insensitive" } },
            { snippet: { contains: "10", mode: "insensitive" } },
            { contactEmail: { contains: "10", mode: "insensitive" } },
            { repEmail: { contains: "10", mode: "insensitive" } },
          ],
        },
        {
          OR: [
            { subject: { contains: "vacation", mode: "insensitive" } },
            { snippet: { contains: "vacation", mode: "insensitive" } },
            { contactEmail: { contains: "vacation", mode: "insensitive" } },
            { repEmail: { contains: "vacation", mode: "insensitive" } },
          ],
        },
      ],
    });
  });

  it("不正な日付形式のbefore:/after:はignoredTermsに(reason=invalidValue、訂正ヒント付きで)積まれて無視される", () => {
    const result = parseEmailLogQuery("before:not-a-date subject:見積", NOW);
    expect(result.ignoredTerms).toEqual([
      { raw: "before:not-a-date", reason: "invalidValue", hint: "日付は YYYY/MM/DD 形式で入力してください" },
    ]);
    expect(result.where).toEqual({ subject: { contains: "見積", mode: "insensitive" } });
  });

  it("不正な形式のolder_than:はignoredTermsに(reason=invalidValue、訂正ヒント付きで)積まれて無視される", () => {
    const result = parseEmailLogQuery("older_than:abc", NOW);
    expect(result.ignoredTerms).toEqual([
      { raw: "older_than:abc", reason: "invalidValue", hint: "d(日)/m(月)/y(年)の数字と単位で指定してください(例: 7d)" },
    ]);
    expect(result.where).toBeNull();
  });

  it("暦上ありえない日付(2026/02/31)はinvalidValueとして無視される(自動繰り上げを許さない)", () => {
    const result = parseEmailLogQuery("after:2026/02/31", NOW);
    expect(result.ignoredTerms).toEqual([
      { raw: "after:2026/02/31", reason: "invalidValue", hint: "日付は YYYY/MM/DD 形式で入力してください" },
    ]);
    expect(result.where).toBeNull();
  });

  it("閏年の2/29は有効な日付として扱われる", () => {
    const result = parseEmailLogQuery("after:2028/02/29", NOW);
    expect(result.ignoredTerms).toEqual([]);
    expect(result.where).toEqual({ sentAt: { gte: new Date("2028-02-29T00:00:00+09:00") } });
  });

  it("閏年でない年の2/29はinvalidValueとして無視される", () => {
    const result = parseEmailLogQuery("after:2026/02/29", NOW);
    expect(result.ignoredTerms).toEqual([
      { raw: "after:2026/02/29", reason: "invalidValue", hint: "日付は YYYY/MM/DD 形式で入力してください" },
    ]);
    expect(result.where).toBeNull();
  });

  it("older_than:1mは遷移先の月に同じ日が無い場合、月末日にクランプする(2026-05-31基準で2026-04-30)", () => {
    const now = new Date("2026-05-31T00:00:00Z");
    const result = parseEmailLogQuery("older_than:1m", now);
    expect(result.where).toEqual({ sentAt: { lt: new Date("2026-04-30T00:00:00Z") } });
  });

  it("older_than:1yは閏年の2/29基準だと2/28にクランプする(2028-02-29基準で2027-02-28)", () => {
    const now = new Date("2028-02-29T00:00:00Z");
    const result = parseEmailLogQuery("older_than:1y", now);
    expect(result.where).toEqual({ sentAt: { lt: new Date("2027-02-28T00:00:00Z") } });
  });

  it("未知のfield:らしき記法(URL等)は自由語として扱われる", () => {
    const result = parseEmailLogQuery("http://example.com", NOW);
    expect(result.ignoredTerms).toEqual([]);
    expect(result.where).toEqual({
      OR: [
        { subject: { contains: "http://example.com", mode: "insensitive" } },
        { snippet: { contains: "http://example.com", mode: "insensitive" } },
        { contactEmail: { contains: "http://example.com", mode: "insensitive" } },
        { repEmail: { contains: "http://example.com", mode: "insensitive" } },
      ],
    });
  });

  it("閉じ括弧が無い等の壊れた構文でも例外を投げず、パースできた範囲を返す", () => {
    expect(() => parseEmailLogQuery("(from:amy", NOW)).not.toThrow();
    expect(() => parseEmailLogQuery("subject:見積)", NOW)).not.toThrow();
    expect(() => parseEmailLogQuery('subject:"unterminated', NOW)).not.toThrow();
    expect(() => parseEmailLogQuery("{from:amy", NOW)).not.toThrow();
  });

  it("フィールド値が空の場合はそのフィールドは無視される", () => {
    const result = parseEmailLogQuery("subject: from:", NOW);
    expect(result.where).toBeNull();
    expect(result.ignoredTerms).toEqual([]);
  });

  // shirokuma-secレビューBLOCKER回帰テスト(2026-08-26): `(`ごとにparseOr→parseAnd→
  // parseTerm→parsePrimaryを再帰呼び出しするパーサに対し、`/email-log?q=`に`(`を
  // 3000個並べただけの入力(通常のURL長制限内)を与えると、対策前は
  // RangeError(Maximum call stack size exceeded)を投げてServer Component全体を
  // 落としていた。MAX_NESTING_DEPTHで再帰の深さを打ち切ることで例外を防ぐ。
  it("括弧が3000段ネストしていてもスタックオーバーフローせずパースできる", () => {
    const deeplyNested = "(".repeat(3000) + "from:amy" + ")".repeat(3000);
    expect(() => parseEmailLogQuery(deeplyNested, NOW)).not.toThrow();
  });

  // MAX_QUERY_LENGTH(500文字)以内に収まる深さでも、MAX_NESTING_DEPTHを超えた分は
  // 再帰せずスキップされ、その旨がignoredTermsで利用者に伝わることを確認する
  // (上の3000段ケースは切り詰めが先に働くため、この検証はここで行う)。
  it("ネスト上限を超えた括弧は再帰せずスキップし、無視した旨を通知する", () => {
    const nested = "(".repeat(200) + "from:amy" + ")".repeat(200);
    const result = parseEmailLogQuery(nested, NOW);
    expect(
      result.ignoredTerms.some((t) => t.reason === "unsupported" && t.raw === "(ネストが深すぎる条件)"),
    ).toBe(true);
  });

  it("波括弧が3000段ネストしていてもスタックオーバーフローせずパースできる", () => {
    const deeplyNested = "{".repeat(3000) + "from:amy" + "}".repeat(3000);
    expect(() => parseEmailLogQuery(deeplyNested, NOW)).not.toThrow();
  });

  it("ネスト深さが上限以内であれば通常通りパースできる", () => {
    const nested = "(".repeat(10) + "from:amy" + ")".repeat(10);
    const result = parseEmailLogQuery(nested, NOW);
    expect(result.ignoredTerms).toEqual([]);
    expect(result.where).toEqual({
      OR: [
        { direction: "inbound", contactEmail: { contains: "amy", mode: "insensitive" } },
        { direction: "outbound", repEmail: { contains: "amy", mode: "insensitive" } },
      ],
    });
  });

  it("qが長すぎる場合は先頭で切り詰めてパースし、truncated=trueを返す", () => {
    const longQuery = `subject:見積 ${"a".repeat(600)}`;
    const result = parseEmailLogQuery(longQuery, NOW);
    expect(result.truncated).toBe(true);
  });

  it("qがMAX_QUERY_LENGTH以内であればtruncated=false", () => {
    const result = parseEmailLogQuery("from:amy", NOW);
    expect(result.truncated).toBe(false);
  });

  it("検索ヘルプにAND演算子が明記されている(演算子として消費されるのに未記載だと無音の取りこぼしになるため)", () => {
    const hasAndHelp = EMAIL_LOG_SEARCH_HELP.some((item) => item.operator.includes("AND"));
    expect(hasAndHelp).toBe(true);
  });
});

// 長すぎるクエリの切り詰め位置(shirokuma-sec検証レビューWARN対応、2026-08-26)。
// 単純な文字数スライスだと`from:secretuser@example.com`が`from:secretuse`に化け、
// ユーザーが意図したより広い部分一致検索が黙って実行されてしまう。
describe("MAX_QUERY_LENGTH超過時の切り詰め", () => {
  it("フィールド値の途中で切らず、その条件ごと落とす", () => {
    const query = "a".repeat(485) + " from:secretuser@example.com";
    const result = parseEmailLogQuery(query, NOW);
    expect(result.truncated).toBe(true);
    // 途中で切れた`secretuse`が部分一致条件として残っていないこと
    expect(JSON.stringify(result.where ?? {})).not.toContain("secretuse");
  });

  it("切り詰め位置より前の完全なトークンは検索条件として残る", () => {
    const result = parseEmailLogQuery("hello " + "b".repeat(600), NOW);
    expect(result.truncated).toBe(true);
    expect(JSON.stringify(result.where ?? {})).toContain("hello");
  });

  it("空白を含まない単一トークンが上限を超える場合は検索条件なしになる", () => {
    const result = parseEmailLogQuery("c".repeat(600), NOW);
    expect(result.truncated).toBe(true);
    expect(result.where).toBeNull();
  });
});
