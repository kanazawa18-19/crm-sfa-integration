import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import {
  BackendApiError,
  getErrorMessage,
  getRevenueTargetSheetSettings,
  saveRevenueTargetSheetSettings,
} from "@/lib/backend";

export async function GET() {
  try {
    const result = await getRevenueTargetSheetSettings();
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}

export async function POST(request: NextRequest) {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ detail: "リクエストボディがJSONとして不正です" }, { status: 400 });
  }

  if (
    typeof payload !== "object" ||
    payload === null ||
    typeof (payload as { spreadsheet_url_or_id?: unknown }).spreadsheet_url_or_id !== "string"
  ) {
    return NextResponse.json({ detail: "spreadsheet_url_or_id は必須です" }, { status: 400 });
  }

  const body = payload as {
    spreadsheet_url_or_id: string;
    mrr_sheet_name?: string | null;
    unit_count_sheet_name?: string | null;
  };

  try {
    const result = await saveRevenueTargetSheetSettings({
      spreadsheet_url_or_id: body.spreadsheet_url_or_id,
      mrr_sheet_name: body.mrr_sheet_name ?? null,
      unit_count_sheet_name: body.unit_count_sheet_name ?? null,
    });
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
