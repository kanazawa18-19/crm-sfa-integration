// マイグレーションのセッションロックは、通常アプリ用のpooled接続では保持できない。
import "dotenv/config";
import { defineConfig } from "prisma/config";
import { migrationDatabaseUrl } from "./prisma/migrationDatabaseUrl";

export default defineConfig({
  schema: "prisma/schema.prisma",
  migrations: {
    path: "prisma/migrations",
  },
  datasource: {
    url: migrationDatabaseUrl(process.env),
  },
});
