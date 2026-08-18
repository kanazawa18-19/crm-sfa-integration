"use server";

import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";

export async function disconnectDrive() {
  const user = await requireRole("viewer");
  await prisma.repDriveConnection.deleteMany({ where: { repEmail: user.email } });
  redirect("/settings/drive?disconnected=1");
}
