"use server";

import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";

export async function disconnectGmail() {
  const user = await requireRole("viewer");
  await prisma.repGmailConnection.deleteMany({ where: { repEmail: user.email } });
  redirect("/settings/gmail");
}
