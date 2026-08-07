import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BackendApiError, getErrorMessage, searchProjects } from "@/lib/backend";

export async function GET(request: NextRequest) {
  const q = request.nextUrl.searchParams.get("q") ?? "";

  try {
    const result = await searchProjects(q);
    return NextResponse.json(result);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return NextResponse.json({ detail: getErrorMessage(error) }, { status });
  }
}
