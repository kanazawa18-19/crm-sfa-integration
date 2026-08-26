import type { Prisma } from "@/generated/prisma/client";

// email-log/page.tsxの検索ボックス用、Gmail検索演算子(公式ドキュメント
// https://support.google.com/mail/answer/7190 で確認済み)をEmailLogテーブルへの
// Prisma WhereInputに変換するパーサ(2026-08-26新設、金沢さんからの「Gmail並みに
// 充実させる」要望対応)。
//
// 対応方針: EmailLogテーブルに実在するデータ(contactEmail/repEmail/direction=
// from・to相当、subject、snippet、sentAt)だけで実現できる演算子に限定する。
// has:attachment・label:・is:unread・cc:/bcc:等、保存していないデータに依存する
// 演算子は非対応。ただし未対応の演算子が入力されてもエラーにはせず、その語句を
// ignoredTermsに積んで黙って無視する(Gmailに慣れたユーザーが打ち間違えても検索
// 自体は失敗させない)。ignoredTermsは「演算子自体が非対応(reason=unsupported)」と
// 「演算子は対応しているが値の形式が不正、いわゆるタイポ(reason=invalidValue、
// 訂正ヒント付き)」を区別する(shirokuma-secレビューWARN対応、2026-08-26。
// 前者しか無いと、後者のユーザーが「その演算子は使えない」と誤解しタイポに
// 気づけないため)。呼び出し側(page.tsx)がignoredTermsを画面に表示する。
//
// SQLインジェクション対策: 生SQLは一切組み立てず、値は必ずPrisma WhereInput経由
// (パラメータ化されたクエリビルダ)で渡す。ユーザー入力を文字列結合でSQLに埋め込む
// 処理はこのファイルに存在しない。

// Gmailの演算子としては存在するが、EmailLogに対応するデータが無いため
// 非対応として扱うフィールド名(認識はした上でignoredTermsに記録し、無視する)。
const UNSUPPORTED_FIELDS = new Set([
  "cc",
  "bcc",
  "label",
  "category",
  "has",
  "list",
  "filename",
  "in",
  "is",
  "deliveredto",
  "size",
  "larger",
  "smaller",
  "rfc822msgid",
  "header",
]);

const SUPPORTED_FIELDS = new Set(["from", "to", "subject", "before", "after", "older_than", "newer_than"]);

// 括弧のネスト上限(shirokuma-secレビューBLOCKER対応、2026-08-26)。`(`ごとに
// parseOr→parseAnd→parseTerm→parsePrimaryを再帰呼び出しするため、上限を設けないと
// `/email-log?q=` に`(`を3000個並べただけの入力(URL長制限内)でRangeError
// (Maximum call stack size exceeded)が発生し、Server Componentである
// page.tsx全体が例外で落ちてしまう(汎用エラーページ行き)。通常の検索条件で
// このネスト数に達することはまず無いため、50階層を超えた分は打ち切ってignoredTermsに
// 積む(パースエラーにはしない)。
const MAX_NESTING_DEPTH = 50;

// `q`パラメータの長さ上限(shirokuma-secレビューWARN対応、2026-08-26)。
// `OR from:x1 OR from:x2 ...`のように横に広いクエリを大量に並べられると、Prismaが
// 生成するWHERE句のOR分岐が際限なく膨れ上がるため、通常の利用では十分な長さに
// 制限する。上限を超えた分はエラーにせず切り詰め、切り詰めたことを呼び出し側
// (page.tsx)に伝える。
const MAX_QUERY_LENGTH = 500;

type DateLeaf = { kind: "date"; op: "before" | "after" | "olderThan" | "newerThan"; date: Date };
type StringLeaf = { kind: "string"; op: "from" | "to" | "subject" | "text"; value: string };
type Leaf = DateLeaf | StringLeaf;

// ignoredTermsの内訳(shirokuma-secレビューWARN対応、2026-08-26)。従来は
// 「未対応の演算子(has:/label:等)」と「対応している演算子だが値の形式が不正
// (before:2026/13/01やolder_than:abc等のタイポ)」を同じ警告文で扱っていたが、
// 後者のユーザーは「その演算子自体は使えるのに、値を直せば動く」ことに気づけない
// ため区別する。
export type IgnoredTerm =
  | { raw: string; reason: "unsupported" }
  | { raw: string; reason: "invalidValue"; hint: string };

type QueryNode =
  | { kind: "leaf"; leaf: Leaf }
  | { kind: "not"; child: QueryNode }
  | { kind: "and"; children: QueryNode[] }
  | { kind: "or"; children: QueryNode[] };

type Token =
  | { type: "LPAREN" | "RPAREN" | "LBRACE" | "RBRACE" | "MINUS" }
  | { type: "ATOM" | "QUOTED"; value: string };

function isBoundaryChar(c: string | undefined): boolean {
  return c === undefined || /\s/.test(c) || c === "(" || c === ")" || c === "{" || c === "}" || c === '"';
}

// 括弧・波括弧・引用符は空白が無くても独立したトークンとして扱う
// (例: `subject:(dinner movie)`)。`-`は直後に空白が無い場合のみ否定演算子として扱う
// (Gmailの仕様通り、`- movie`のように空白を挟むと否定にならない)。
function tokenize(input: string): Token[] {
  const tokens: Token[] = [];
  const n = input.length;
  let i = 0;

  while (i < n) {
    const c = input[i];
    if (/\s/.test(c)) {
      i++;
      continue;
    }
    if (c === "(") {
      tokens.push({ type: "LPAREN" });
      i++;
      continue;
    }
    if (c === ")") {
      tokens.push({ type: "RPAREN" });
      i++;
      continue;
    }
    if (c === "{") {
      tokens.push({ type: "LBRACE" });
      i++;
      continue;
    }
    if (c === "}") {
      tokens.push({ type: "RBRACE" });
      i++;
      continue;
    }
    if (c === '"') {
      const end = input.indexOf('"', i + 1);
      const content = end === -1 ? input.slice(i + 1) : input.slice(i + 1, end);
      tokens.push({ type: "QUOTED", value: content });
      i = end === -1 ? n : end + 1;
      continue;
    }
    if (c === "-" && i + 1 < n && !/\s/.test(input[i + 1])) {
      tokens.push({ type: "MINUS" });
      i++;
      continue;
    }

    const start = i;
    while (i < n && !isBoundaryChar(input[i])) i++;
    let atomText = input.slice(start, i);
    // `subject:"foo bar"`のようにfield:の直後に引用符が続く場合は結合して1トークンにする。
    if (atomText.endsWith(":") && input[i] === '"') {
      const end = input.indexOf('"', i + 1);
      const content = end === -1 ? input.slice(i + 1) : input.slice(i + 1, end);
      atomText += content;
      i = end === -1 ? n : end + 1;
    }
    if (atomText.length > 0) {
      tokens.push({ type: "ATOM", value: atomText });
    }
  }
  return tokens;
}

// 閏年を含めた月ごとの実在日数。2月は`isLeapYear`で29/28日を切り替える。
function daysInMonth(year: number, month: number): number {
  const isLeapYear = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  const lengths = [31, isLeapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return lengths[month - 1];
}

function parseGmailDate(raw: string): Date | null {
  const m = raw.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$/);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (month < 1 || month > 12) return null;
  // 月/日をそれぞれ1-31の範囲でチェックするだけでは`2026/02/31`のような暦上
  // ありえない日付を弾けず、Dateコンストラクタが黙って3/3相当へ繰り上げてしまう
  // (shirokuma-secレビューWARN対応、2026-08-26)。実在する日数まで検証する。
  if (day < 1 || day > daysInMonth(year, month)) return null;
  const mm = String(month).padStart(2, "0");
  const dd = String(day).padStart(2, "0");
  // 他画面(audit-log/page.tsx等)と同じく、日付単体はJSTの0時として扱う。
  const date = new Date(`${year}-${mm}-${dd}T00:00:00+09:00`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function parseRelativeDuration(raw: string): { amount: number; unit: "d" | "m" | "y" } | null {
  const m = raw.match(/^(\d+)(d|m|y)$/);
  if (!m) return null;
  const amount = Number(m[1]);
  if (amount <= 0) return null;
  return { amount, unit: m[2] as "d" | "m" | "y" };
}

function subtractDuration(base: Date, amount: number, unit: "d" | "m" | "y"): Date {
  const result = new Date(base.getTime());
  if (unit === "d") {
    result.setUTCDate(result.getUTCDate() - amount);
    return result;
  }
  // 月/年単位は`setUTCMonth`/`setUTCFullYear`が日をそのまま保持するため、遷移先の月に
  // その日が存在しないとロールオーバーする(例: 2026-05-31から1ヶ月引くと、4月には
  // 31日が無いため5月1日まで進んでしまい「4月末のはず」がずれる。2028-02-29から
  // 1年引く場合も同様に2027-02-28のはずが2027-03-01になる)。shirokuma-secレビューWARN
  // 対応(2026-08-26)として、遷移先の月の末日を超えないように日をクランプする。
  const day = result.getUTCDate();
  let targetYear = result.getUTCFullYear();
  let targetMonth = result.getUTCMonth(); // 0-11
  if (unit === "m") {
    targetMonth -= amount;
  } else {
    targetYear -= amount;
  }
  // targetMonthが0-11の範囲外でも、setUTCFullYearが年をまたいで正規化する
  // (月自体のロールオーバーは意図通りの挙動なので許容する。ここで守りたいのは
  // あくまで「日」が実在しない値にならないことだけ)。まず1日固定で月を確定させてから、
  // その月の実日数までにクランプした日を設定する。
  const normalized = new Date(base.getTime());
  normalized.setUTCFullYear(targetYear, targetMonth, 1);
  const maxDay = daysInMonth(normalized.getUTCFullYear(), normalized.getUTCMonth() + 1);
  normalized.setUTCDate(Math.min(day, maxDay));
  return normalized;
}

class Parser {
  private pos = 0;
  // `(`/`{`のネスト深さ。MAX_NESTING_DEPTHを超えたら再帰を打ち切る
  // (shirokuma-secレビューBLOCKER対応)。
  private depth = 0;
  private depthLimitNoted = false;

  constructor(
    private readonly tokens: Token[],
    private readonly ignoredTerms: IgnoredTerm[],
    private readonly now: Date,
  ) {}

  parse(): QueryNode | null {
    return this.parseOr();
  }

  private pushUnsupported(raw: string): void {
    this.ignoredTerms.push({ raw, reason: "unsupported" });
  }

  private pushInvalidValue(raw: string, hint: string): void {
    this.ignoredTerms.push({ raw, reason: "invalidValue", hint });
  }

  // MAX_NESTING_DEPTHに達した状態で新たな`(`/`{`に出会った場合、再帰には入らず
  // (this.posを起点として)対応する閉じ括弧まで反復処理でスキップする。ネストの深さに
  // 比例したスタック消費が発生しないため、どれだけ深いネストが来てもクラッシュしない。
  private skipBalanced(): void {
    let balance = 0;
    while (this.pos < this.tokens.length) {
      const t = this.tokens[this.pos];
      if (t.type === "LPAREN" || t.type === "LBRACE") {
        balance++;
        this.pos++;
      } else if (t.type === "RPAREN" || t.type === "RBRACE") {
        this.pos++;
        if (balance === 0) return;
        balance--;
      } else {
        this.pos++;
      }
    }
  }

  private noteDepthLimitExceeded(): void {
    if (this.depthLimitNoted) return;
    this.depthLimitNoted = true;
    this.pushUnsupported("(ネストが深すぎる条件)");
  }

  private peek(): Token | undefined {
    return this.tokens[this.pos];
  }

  private isOrToken(token: Token | undefined): boolean {
    return !!token && token.type === "ATOM" && token.value === "OR";
  }

  private isAndToken(token: Token | undefined): boolean {
    return !!token && token.type === "ATOM" && token.value === "AND";
  }

  // OR は AND(暗黙のスペース区切り、または明示的なAND)より優先度が低い
  // (Gmail公式ドキュメントの例`from:amy movie OR from:david`に準じる)。
  private parseOr(): QueryNode | null {
    const first = this.parseAnd();
    const children: QueryNode[] = first ? [first] : [];
    while (this.isOrToken(this.peek())) {
      this.pos++;
      const rhs = this.parseAnd();
      if (rhs) children.push(rhs);
    }
    if (children.length === 0) return null;
    return children.length === 1 ? children[0] : { kind: "or", children };
  }

  private parseAnd(): QueryNode | null {
    const children: QueryNode[] = [];
    while (this.peek() && !this.isOrToken(this.peek()) && this.peek()!.type !== "RPAREN" && this.peek()!.type !== "RBRACE") {
      if (this.isAndToken(this.peek())) {
        this.pos++;
        continue;
      }
      const term = this.parseTerm();
      if (term) children.push(term);
    }
    if (children.length === 0) return null;
    return children.length === 1 ? children[0] : { kind: "and", children };
  }

  private parseTerm(): QueryNode | null {
    let negate = false;
    if (this.peek()?.type === "MINUS") {
      this.pos++;
      negate = true;
    }
    const node = this.parsePrimary();
    if (!node) return null;
    return negate ? { kind: "not", child: node } : node;
  }

  private parsePrimary(): QueryNode | null {
    const token = this.peek();
    if (!token) return null;

    if (token.type === "LPAREN") {
      this.pos++;
      if (this.depth >= MAX_NESTING_DEPTH) {
        this.noteDepthLimitExceeded();
        this.skipBalanced();
        return null;
      }
      this.depth++;
      const inner = this.parseOr();
      this.depth--;
      if (this.peek()?.type === "RPAREN") this.pos++;
      return inner;
    }

    if (token.type === "LBRACE") {
      this.pos++;
      if (this.depth >= MAX_NESTING_DEPTH) {
        this.noteDepthLimitExceeded();
        this.skipBalanced();
        return null;
      }
      this.depth++;
      // `{}`内はスペースがOR扱いになる(Gmail公式の`{from:amy from:david}`の例)。
      const children: QueryNode[] = [];
      while (this.peek() && this.peek()!.type !== "RBRACE") {
        if (this.isOrToken(this.peek()) || this.isAndToken(this.peek())) {
          this.pos++;
          continue;
        }
        const term = this.parseTerm();
        if (term) children.push(term);
      }
      this.depth--;
      if (this.peek()?.type === "RBRACE") this.pos++;
      if (children.length === 0) return null;
      return children.length === 1 ? children[0] : { kind: "or", children };
    }

    if (token.type === "QUOTED") {
      this.pos++;
      const value = token.value.trim();
      return value ? { kind: "leaf", leaf: { kind: "string", op: "text", value } } : null;
    }

    if (token.type === "ATOM") {
      this.pos++;
      return this.buildLeafFromAtom(token.value);
    }

    // RPAREN/RBRACEが単独で出てきた場合(構文が崩れている)は何も生成しない。
    return null;
  }

  private buildLeafFromAtom(raw: string): QueryNode | null {
    if (raw === "OR" || raw === "AND") return null;
    if (raw === "AROUND") {
      // 近接検索(word1 AROUND N word2)は本文全文が無いと意味を持たないため非対応。
      this.pushUnsupported(raw);
      return null;
    }

    const m = raw.match(/^([A-Za-z_]+):([\s\S]*)$/);
    if (m) {
      const field = m[1].toLowerCase();
      let value = m[2];
      if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
        value = value.slice(1, -1);
      }
      value = value.trim();

      if (SUPPORTED_FIELDS.has(field)) {
        if (field === "from" || field === "to") {
          return value ? { kind: "leaf", leaf: { kind: "string", op: field, value } } : null;
        }
        if (field === "subject") {
          return value ? { kind: "leaf", leaf: { kind: "string", op: "subject", value } } : null;
        }
        if (field === "before" || field === "after") {
          const date = value ? parseGmailDate(value) : null;
          if (!date) {
            // 演算子自体は対応しているが値の形式が不正なケース(shirokuma-secレビューWARN
            // 対応、2026-08-26)。「対応していない」ではなく訂正ヒントを出す。
            this.pushInvalidValue(raw, "日付は YYYY/MM/DD 形式で入力してください");
            return null;
          }
          return { kind: "leaf", leaf: { kind: "date", op: field, date } };
        }
        // older_than / newer_than
        const duration = value ? parseRelativeDuration(value) : null;
        if (!duration) {
          this.pushInvalidValue(raw, "d(日)/m(月)/y(年)の数字と単位で指定してください(例: 7d)");
          return null;
        }
        const cutoff = subtractDuration(this.now, duration.amount, duration.unit);
        return {
          kind: "leaf",
          leaf: { kind: "date", op: field === "older_than" ? "olderThan" : "newerThan", date: cutoff },
        };
      }

      if (UNSUPPORTED_FIELDS.has(field)) {
        this.pushUnsupported(raw);
        return null;
      }
      // 未知の"foo:bar"らしき記法(URL等)はフィールド指定とみなさず自由語として扱う。
    }

    const text = raw.trim();
    return text ? { kind: "leaf", leaf: { kind: "string", op: "text", value: text } } : null;
  }
}

function buildWhere(node: QueryNode): Prisma.EmailLogWhereInput {
  switch (node.kind) {
    case "and":
      return { AND: node.children.map(buildWhere) };
    case "or":
      return { OR: node.children.map(buildWhere) };
    case "not":
      return { NOT: buildWhere(node.child) };
    case "leaf":
      return buildLeafWhere(node.leaf);
  }
}

// 以下のcontains(=ILIKE '%...%'相当)は前方一致ではないためインデックスを
// 活かせない(shirokuma-secレビュー、記録のみでOK扱い、2026-08-26)。本格的に
// 対応するにはpg_trgmのGINインデックス導入が必要だが、現状のEmailLog件数では
// 未着手でも支障が無いため今回のスコープ外とする。件数が増えて遅くなってきたら
// 検討する。
function buildLeafWhere(leaf: Leaf): Prisma.EmailLogWhereInput {
  if (leaf.kind === "date") {
    switch (leaf.op) {
      case "after":
      case "newerThan":
        return { sentAt: { gte: leaf.date } };
      case "before":
      case "olderThan":
        return { sentAt: { lt: leaf.date } };
    }
  }

  switch (leaf.op) {
    case "subject":
      return { subject: { contains: leaf.value, mode: "insensitive" } };
    case "text":
      // 本文全文は保存していないため、件名・スニペット・連絡先/担当者メールアドレスを
      // 横断的に検索する(Gmailの無指定検索が全フィールドを横断するのに準じた設計)。
      return {
        OR: [
          { subject: { contains: leaf.value, mode: "insensitive" } },
          { snippet: { contains: leaf.value, mode: "insensitive" } },
          { contactEmail: { contains: leaf.value, mode: "insensitive" } },
          { repEmail: { contains: leaf.value, mode: "insensitive" } },
        ],
      };
    case "from":
      // EmailLogには送信者/宛先を直接示す列が無く、direction("inbound"|"outbound")と
      // contactEmail/repEmailの組み合わせで送信者・宛先が決まる
      // (inbound: 連絡先→担当者、outbound: 担当者→連絡先)。
      return {
        OR: [
          { direction: "inbound", contactEmail: { contains: leaf.value, mode: "insensitive" } },
          { direction: "outbound", repEmail: { contains: leaf.value, mode: "insensitive" } },
        ],
      };
    case "to":
      return {
        OR: [
          { direction: "inbound", repEmail: { contains: leaf.value, mode: "insensitive" } },
          { direction: "outbound", contactEmail: { contains: leaf.value, mode: "insensitive" } },
        ],
      };
  }
}

export interface EmailLogQueryParseResult {
  where: Prisma.EmailLogWhereInput | null;
  ignoredTerms: IgnoredTerm[];
  // trueの場合、qパラメータがMAX_QUERY_LENGTHを超えていたため先頭を切り詰めて
  // パースした(shirokuma-secレビューWARN対応、2026-08-26)。
  truncated: boolean;
}

// MAX_QUERY_LENGTH超過時の切り詰め位置を、トークンの途中で切れないよう空白境界まで戻す
// (shirokuma-sec検証レビューWARN対応、2026-08-26)。単純に`slice(0, MAX_QUERY_LENGTH)`すると
// 例えば`from:secretuser@example.com`が`from:secretuse`に化け、ユーザーが意図したより広い
// 部分一致検索が黙って実行される(意図しないメールがヒットする)。最後の空白以降を丸ごと
// 捨てることで「条件が中途半端に化ける」のではなく「その条件ごと落ちる」形にし、安全側へ倒す。
// 空白が1つも無い場合(1トークンが500文字超)はその1トークンごと捨てて空クエリにする。
function truncateAtTokenBoundary(query: string): string {
  const sliced = query.slice(0, MAX_QUERY_LENGTH);
  const lastSpace = sliced.search(/\s\S*$/);
  return lastSpace === -1 ? "" : sliced.slice(0, lastSpace);
}

export function parseEmailLogQuery(query: string, now: Date = new Date()): EmailLogQueryParseResult {
  const truncated = query.length > MAX_QUERY_LENGTH;
  const effectiveQuery = truncated ? truncateAtTokenBoundary(query) : query;
  const ignoredTerms: IgnoredTerm[] = [];
  const tokens = tokenize(effectiveQuery);
  const ast = new Parser(tokens, ignoredTerms, now).parse();
  return { where: ast ? buildWhere(ast) : null, ignoredTerms, truncated };
}

// email-log/page.tsxのヘルプ表示用。対応している演算子のみを載せる
// (has:/label:/is:/cc:/bcc:/category:/filename:等はEmailLogに対応データが無く非対応)。
export const EMAIL_LOG_SEARCH_HELP: { operator: string; description: string; example: string }[] = [
  { operator: "from:", description: "送信者のメールアドレス", example: "from:example.com" },
  { operator: "to:", description: "宛先のメールアドレス", example: "to:example.com" },
  { operator: "subject:", description: "件名に含まれる文字列", example: "subject:御見積" },
  { operator: "after: / before:", description: "指定日以降 / より前(YYYY/MM/DD)", example: "after:2026/08/01 before:2026/09/01" },
  { operator: "older_than: / newer_than:", description: "現在からd(日)/m(月)/y(年)単位で絞り込み", example: "newer_than:7d" },
  { operator: '"..."', description: "フレーズの完全一致", example: '"dinner and movie"' },
  { operator: "OR / AND", description: "いずれかの条件に一致 / 両方の条件に一致(要:大文字。省略した場合はANDと同じ扱い)", example: "from:amy OR from:david" },
  { operator: "-", description: "指定した条件を除外(直前にスペースを入れない)", example: "-subject:テスト" },
  { operator: "( ) { }", description: "条件のグループ化({}内はスペースがOR扱い)", example: "(from:amy OR from:david) subject:見積" },
];
