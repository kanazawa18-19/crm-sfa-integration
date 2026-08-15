// web-engagement-toolのprisma.config.tsと同じ構成(2026-08-15、Prisma 7ではmigrate系
// コマンドがこのファイルからdatasource URLを読むようになったため必須)。
import "dotenv/config";
import { defineConfig } from "prisma/config";

export default defineConfig({
  schema: "prisma/schema.prisma",
  migrations: {
    path: "prisma/migrations",
  },
  datasource: {
    url: process.env["DATABASE_URL"],
  },
});
