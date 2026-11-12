import { PrismaClient } from "@/generated/prisma/client";
import { PrismaPg } from "@prisma/adapter-pg";
import { databaseConnectionString } from "@/lib/databaseConnectionString";

declare const globalThis: {
  prismaGlobal?: PrismaClient;
} & typeof global;

function createPrismaClient() {
  const adapter = new PrismaPg({
    connectionString: databaseConnectionString(process.env.DATABASE_URL),
  });
  return new PrismaClient({ adapter });
}

const prisma = globalThis.prismaGlobal ?? createPrismaClient();

if (process.env.NODE_ENV !== "production") {
  globalThis.prismaGlobal = prisma;
}

export default prisma;
