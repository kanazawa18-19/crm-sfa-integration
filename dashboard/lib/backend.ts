// バックエンドAPI（FastAPI）とのやり取りはこのファイル経由でのみ行う。
// BACKEND_API_TOKEN はサーバーサイド（Server Component / Route Handler）でのみ
// 参照され、ブラウザに渡ることはない。

export class BackendApiError extends Error {
  status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "BackendApiError";
    this.status = status;
  }
}

export function getErrorMessage(error: unknown): string {
  if (error instanceof BackendApiError || error instanceof Error) {
    return error.message;
  }
  return "不明なエラーが発生しました";
}

async function fetchBackend<T>(
  path: string,
  options?: { method?: "GET" | "POST"; body?: unknown }
): Promise<T> {
  const baseUrl = process.env.BACKEND_API_URL;
  if (!baseUrl) {
    throw new BackendApiError("BACKEND_API_URL が設定されていません");
  }
  const token = process.env.BACKEND_API_TOKEN;

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method: options?.method ?? "GET",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options?.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: options?.body !== undefined ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
    });
  } catch (error) {
    throw new BackendApiError(
      `バックエンドAPIへの接続に失敗しました: ${error instanceof Error ? error.message : String(error)}`
    );
  }

  if (!response.ok) {
    // バックエンドはエラー時にJSON {"detail": "..."} を返す（TemplateNotFoundError等、
    // 利用者が次に何をすればよいか分かる日本語メッセージが入っている）。これを握りつぶして
    // 汎用メッセージに丸めると、書類生成失敗時等に重要な情報が失われる
    // （obasan-qualityレビュー: search/generateでエラー処理方針が非対称との指摘を反映し、
    // fetchBackend経由の呼び出し元も含めて統一的にdetailを優先するようにした）。
    const body = await response.json().catch(() => null);
    const detail = body && typeof body.detail === "string" ? body.detail : null;
    throw new BackendApiError(
      detail ?? `バックエンドAPIがエラーを返しました（status: ${response.status}）`,
      response.status
    );
  }

  return (await response.json()) as T;
}

// クオーター/半期/通期のいずれか1期間分の着地予測。会計年度は12月始まり・11月末
// （src/analytics/fiscal_calendar.py参照）で、rangeもその会計期間に基づく。
export interface ForecastPeriod {
  range: { start: string; end: string };
  max: { initial_fee: number; mrr: number };
  expected: { initial_fee: number; mrr: number };
  min: { initial_fee: number; mrr: number };
}

/**
 * ダッシュボードのトップページ用サマリー。
 *
 * 案件の期間帰属は、Notion上の単一プロパティ「契約日 / 予想契約日」を、契約済案件は
 * 実際の契約日として・進行中案件は営業担当が入力した予想契約日として、それぞれ読んで
 * 判定している（1つのプロパティが案件のステータスによって意味を変える、やや非直感的な
 * 設計）。この日付が未入力の案件はforecastのいずれの期間にも計上されず、
 * unscheduled_active_count/unscheduled_confirmed_countとして件数のみ別集計される
 * （詳細な理由・注記文はnotesに入る）。半期・通期の実績にはクオーター分の数字も
 * 含まれる（累積であり、3期間は互いに独立した数字ではない）。
 * 詳細なビジネスルールは`src/api/dashboard_service.py`の`build_dashboard_summary`
 * docstringを参照。
 */
export interface DashboardSummary {
  as_of: string;
  forecast: {
    quarter: ForecastPeriod;
    half: ForecastPeriod;
    year: ForecastPeriod;
    unscheduled_active_count: number;
    unscheduled_confirmed_count: number;
  };
  notes: string[];
  status_breakdown: Array<{
    status: string;
    category: string;
    count: number;
    initial_fee_sum: number;
    monthly_fee_sum: number;
  }>;
  totals: {
    project_count: number;
    confirmed_count: number;
    active_count: number;
    lost_count: number;
    cancelled_count: number;
  };
}

export function getDashboardSummary(): Promise<DashboardSummary> {
  return fetchBackend<DashboardSummary>("/api/dashboard/summary");
}

export interface DailyReport {
  report_date: string;
  next_business_day: string;
  member_summaries: Array<{
    member: string;
    counts_by_type: Record<string, number>;
    total: number;
  }>;
  new_projects: Array<{
    client_name: string;
    proposed_services: string[];
    initial_fee: number;
    monthly_fee: number;
    assignee: string;
  }>;
  status_changes: unknown[];
  upcoming_actions: Array<{
    client_name: string;
    assignee: string;
    next_action_date: string;
  }>;
  notes: string[];
}

export function getDailyReport(date: string): Promise<DailyReport> {
  return fetchBackend<DailyReport>(`/api/reports/daily?date=${encodeURIComponent(date)}`);
}

export interface MemberPerformance {
  member: string;
  volume_contact_count: number;
  volume_score: number | null;
  quality_win_rate: number | null;
  speed_compliance_rate: number | null;
  overall_score: number | null;
}

export interface MembersPerformanceResponse {
  as_of: string;
  members: MemberPerformance[];
  notes: string[];
}

export function getMembersPerformance(asOf?: string): Promise<MembersPerformanceResponse> {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return fetchBackend<MembersPerformanceResponse>(`/api/members/performance${query}`);
}

export interface ProjectSearchResult {
  notion_page_id: string;
  project_name: string;
  status: string | null;
  proposed_services: string[];
}

export interface ProjectSearchResponse {
  projects: ProjectSearchResult[];
  total_matched: number;
}

export function searchProjects(query: string): Promise<ProjectSearchResponse> {
  return fetchBackend<ProjectSearchResponse>(`/api/projects/search?q=${encodeURIComponent(query)}`);
}

// 顧客360度ビュー（取引先1社の案件・連絡先・アクション履歴・メール履歴・変更履歴を1画面に
// 集約する営業向け画面）。取引先検索・連絡先検索は`src/api/client_360_service.py`の
// `search_clients`/`search_contacts`と同じくNotion API側で絞り込んだ先頭件のみを返す
// （取引先マスターDB・連絡先DBは大規模なため全件は返さない）。

export interface ClientSearchResult {
  notion_page_id: string;
  取引先名: string;
}

export interface ClientSearchResponse {
  clients: ClientSearchResult[];
  // 検索結果が上限件数(バックエンド側`_MAX_SEARCH_RESULTS`)を超えて一致した可能性が
  // あるかどうか。`query_page()`は1回のクエリで打ち切る設計のため正確な一致件数は
  // 分からず、「あるかないか」のみ返す(obasan-qualityレビューBLOCKER対応、2026-08-18。
  // 元は`total_matched = clients.length`が常に成り立ち、「他に◯件該当」表示が
  // 実質デッドコードになっていた)。
  truncated: boolean;
}

export function searchClients(query: string): Promise<ClientSearchResponse> {
  return fetchBackend<ClientSearchResponse>(`/api/clients/search?q=${encodeURIComponent(query)}`);
}

export interface ContactSearchResult {
  notion_page_id: string;
  名前: string;
}

export interface ContactSearchResponse {
  contacts: ContactSearchResult[];
  truncated: boolean;
}

// 現行UIの360ビューは取引先検索から入るのみで連絡先検索は未使用だが、将来の拡張用に
// バックエンド側の`/api/contacts/search`ラッパーとして残しておく。
export function searchContacts(query: string): Promise<ContactSearchResponse> {
  return fetchBackend<ContactSearchResponse>(`/api/contacts/search?q=${encodeURIComponent(query)}`);
}

// `src/api/client_360_service.py`の各`page_to_display_dict`/`project_page_to_mirror_record`
// はスキーマ全プロパティを表示用dictへ変換して返すため、実際のレスポンスにはここで挙げた
// 項目以外も含まれる。360ビューで実際に表示する項目のみ明示的に型付けし、それ以外は
// インデックスシグネチャで受け止める。
export interface Client360Client {
  notion_page_id: string;
  取引先名: string;
  顧客種別: string | null;
  都道府県: string | null;
  住所: string | null;
  TEL: string | null;
  FAX: string | null;
  備考: string | null;
  [key: string]: unknown;
}

export interface Client360Project {
  notion_page_id: string;
  案件名: string;
  営業ステータス: string | null;
  確度: string | null;
  初期費用: number | null;
  月額費用: number | null;
  担当メンバー: string[];
  次回アクション日: string | null;
  提案サービス: string[];
  [key: string]: unknown;
}

export interface Client360Contact {
  notion_page_id: string;
  名前: string;
  部署: string | null;
  役職: string | null;
  メールアドレス: string | null;
  携帯番号: string | null;
  直通TEL: string | null;
  担当メンバー: unknown;
  [key: string]: unknown;
}

export interface Client360Action {
  notion_page_id: string;
  "商談回数・電話回数・メール回数（何回目）": string | null;
  アクション種別: string | null;
  アクション日: string | null;
  履歴メモ: string | null;
  先方担当者: string | null;
  担当営業: string | null;
  [key: string]: unknown;
}

// 連絡先ごとの返信傾向(2026-09-03、src/api/reply_timing_service.py)。
// 「返信ラグ」と「返ってきやすい時間帯」はサンプル数が別物(前者は送信→受信の
// ペア数、後者は受信の総数)のため、confidence/sample_sizeをそれぞれが持つ。
export interface ReplyTimingWindow {
  label: string;
  count: number;
}

export type ReplyTimingConfidence = "high" | "medium" | "low" | "none";

export interface ReplyTiming {
  // 返信ラグ側のサンプル数(送信→受信のペア数)。
  sample_size: number;
  confidence: ReplyTimingConfidence;
  confidence_label: string;
  median_lag_seconds: number | null;
  median_lag_label: string;
  mean_lag_seconds: number | null;
  mean_lag_label: string;
  fastest_lag_label: string;
  slowest_lag_label: string;
  inbound_count: number;
  outbound_count: number;
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  timing: {
    sample_size: number;
    confidence: ReplyTimingConfidence;
    confidence_label: string;
    top_buckets: ReplyTimingWindow[];
    top_weekdays: string[];
    buckets: ReplyTimingWindow[];
    weekday_counts: number[];
  };
  note: string;
}

export interface Client360 {
  client: Client360Client;
  projects: Client360Project[];
  contacts: Client360Contact[];
  actions: Client360Action[];
  // 連絡先のNotionページIDをキーにした返信傾向。ログが1件も無い連絡先はキー自体が
  // 存在しない(バックエンド側が0件のダミーを返さない設計)。
  reply_timing: Record<string, ReplyTiming>;
}

export function getClient360(clientId: string): Promise<Client360> {
  return fetchBackend<Client360>(`/api/clients/${encodeURIComponent(clientId)}/360`);
}

export interface Task {
  notion_page_id: string;
  title_summary: string;
  status: string | null;
  due_date: string | null;
  is_overdue: boolean;
  assignees: string[];
  ball: string[];
  category: string[];
  tags: string[];
  has_project_link: boolean;
}

export interface TasksResponse {
  as_of: string;
  tasks: Task[];
  overdue_count: number;
  total_count: number;
}

export function getTasks(): Promise<TasksResponse> {
  return fetchBackend<TasksResponse>("/api/tasks");
}

export interface ManagerAlertEntry {
  notion_page_id: string;
  project_name: string;
  assignee: string;
  status: string;
  confidence: string;
  next_action_date: string | null;
  reason: string;
  is_proxy: boolean;
}

export interface ManagerAlertsResponse {
  as_of: string;
  alerts: {
    lost: ManagerAlertEntry[];
    lost_candidate: ManagerAlertEntry[];
    stalled: ManagerAlertEntry[];
    won: ManagerAlertEntry[];
  };
  counts: {
    lost: number;
    lost_candidate: number;
    stalled: number;
    won: number;
  };
  stalled_days_threshold: number;
  notes: string[];
}

export function getManagerAlerts(asOf?: string): Promise<ManagerAlertsResponse> {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return fetchBackend<ManagerAlertsResponse>(`/api/alerts/manager${query}`);
}

// 事業計画スプレッドシート連携設定（値そのものはNotion側に複製せず、スプレッドシートへの
// ポインタのみを保存する。src/reports/revenue_target_sheet.py・
// src/reports/revenue_target_settings.py のモジュールdocstring参照）。
export interface RevenueTargetSheetPointer {
  spreadsheet_id: string;
  mrr_sheet_name: string | null;
  unit_count_sheet_name: string | null;
}

export interface RevenueTargetSheetSettings {
  configured: boolean;
  pointer: RevenueTargetSheetPointer | null;
  updated_at: string | null;
}

export function getRevenueTargetSheetSettings(): Promise<RevenueTargetSheetSettings> {
  return fetchBackend<RevenueTargetSheetSettings>("/api/settings/revenue-target-sheet");
}

export interface SaveRevenueTargetSheetSettingsRequest {
  spreadsheet_url_or_id: string;
  mrr_sheet_name: string | null;
  unit_count_sheet_name: string | null;
}

export interface SaveRevenueTargetSheetSettingsResponse {
  pointer: RevenueTargetSheetPointer;
  updated_at: string;
  validation_success: boolean;
  validation_error: string | null;
  mrr_month_count: number | null;
  unit_count_month_count: number | null;
}

// 見積書 承認フロー(2026-08-18)。承認者一覧(DocumentApprover)はこのdashboard側の
// Prismaで直接扱う(下記documents/page.tsxのServer Component参照)ため、ここでは
// バックエンドの request-approval エンドポイントのみをラップする。
// 書類作成画面の手動入力欄(2026-08-19追加)。全項目任意。
export interface QuoteOverridesInput {
  memo?: string;
  clientName?: string;
  serviceName?: string;
  initialFee?: string;
  monthlyFee?: string;
  creatorName?: string;
}

export interface QuoteApprovalRequest {
  projectId: string;
  approverEmails: string[];
  requestedByEmail: string;
  message?: string;
  overrides?: QuoteOverridesInput;
}

export interface QuoteApprovalResponse {
  drive_file_id: string;
  drive_approval_id: string;
  document_approval_id: string;
}

export function requestQuoteApproval(
  request: QuoteApprovalRequest
): Promise<QuoteApprovalResponse> {
  return fetchBackend<QuoteApprovalResponse>("/api/documents/quote/request-approval", {
    method: "POST",
    body: {
      project_id: request.projectId,
      approver_emails: request.approverEmails,
      requested_by_email: request.requestedByEmail,
      message: request.message ?? "",
      memo: request.overrides?.memo,
      client_name: request.overrides?.clientName,
      service_name: request.overrides?.serviceName,
      initial_fee: request.overrides?.initialFee,
      monthly_fee: request.overrides?.monthlyFee,
      creator_name: request.overrides?.creatorName,
    },
  });
}

export function saveRevenueTargetSheetSettings(
  payload: SaveRevenueTargetSheetSettingsRequest
): Promise<SaveRevenueTargetSheetSettingsResponse> {
  return fetchBackend<SaveRevenueTargetSheetSettingsResponse>("/api/settings/revenue-target-sheet", {
    method: "POST",
    body: payload,
  });
}
