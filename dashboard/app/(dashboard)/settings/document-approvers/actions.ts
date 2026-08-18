"use server";

import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";

// 見積書の承認者候補(平本さん・黒井さん等)の管理(2026-08-18)。DocumentApproverは
// このdashboard側のPrisma CRUDで完結する(RepGmailConnection等と異なりPythonバックエンド側は
// 読み書きしない——承認リクエスト送信時は選択済みのapprover_emailのみが渡される)。
// app/(dashboard)/users/page.tsxのServer Actionパターンをそのまま踏襲する。

export async function createDocumentApprover(formData: FormData) {
  await requireRole("master");

  const name = String(formData.get("name") ?? "").trim();
  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  const title = String(formData.get("title") ?? "").trim();
  if (!name || !email) return;

  await prisma.documentApprover.create({
    data: { name, email, title: title || null },
  });
  redirect("/settings/document-approvers");
}

export async function toggleDocumentApproverActive(formData: FormData) {
  await requireRole("master");

  const id = String(formData.get("id") ?? "");
  if (!id) return;

  const current = await prisma.documentApprover.findUnique({ where: { id } });
  if (!current) return;

  await prisma.documentApprover.update({ where: { id }, data: { active: !current.active } });
  redirect("/settings/document-approvers");
}

export async function deleteDocumentApprover(formData: FormData) {
  await requireRole("master");

  const id = String(formData.get("id") ?? "");
  if (!id) return;

  await prisma.documentApprover.delete({ where: { id } }).catch(() => null);
  redirect("/settings/document-approvers");
}
