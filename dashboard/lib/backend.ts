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

async function fetchBackend<T>(path: string): Promise<T> {
  const baseUrl = process.env.BACKEND_API_URL;
  if (!baseUrl) {
    throw new BackendApiError("BACKEND_API_URL が設定されていません");
  }
  const token = process.env.BACKEND_API_TOKEN;

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      cache: "no-store",
    });
  } catch (error) {
    throw new BackendApiError(
      `バックエンドAPIへの接続に失敗しました: ${error instanceof Error ? error.message : String(error)}`
    );
  }

  if (!response.ok) {
    throw new BackendApiError(
      `バックエンドAPIがエラーを返しました（status: ${response.status}）`,
      response.status
    );
  }

  return (await response.json()) as T;
}

export interface DashboardSummary {
  as_of: string;
  forecast: {
    max: { initial_fee: number; mrr: number };
    expected: { initial_fee: number; mrr: number };
    min: { initial_fee: number; mrr: number };
  };
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
