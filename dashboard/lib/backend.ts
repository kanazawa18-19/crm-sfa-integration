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

export function saveRevenueTargetSheetSettings(
  payload: SaveRevenueTargetSheetSettingsRequest
): Promise<SaveRevenueTargetSheetSettingsResponse> {
  return fetchBackend<SaveRevenueTargetSheetSettingsResponse>("/api/settings/revenue-target-sheet", {
    method: "POST",
    body: payload,
  });
}
