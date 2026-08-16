-- CreateTable
CREATE TABLE "AuditLog" (
    "id" TEXT NOT NULL,
    "dbKey" TEXT NOT NULL,
    "notionPageId" TEXT NOT NULL,
    "action" TEXT NOT NULL,
    "changedFields" JSONB NOT NULL,
    "actorSource" TEXT NOT NULL,
    "actorLabel" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "AuditLog_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "AuditLog_dbKey_idx" ON "AuditLog"("dbKey");

-- CreateIndex
CREATE INDEX "AuditLog_notionPageId_idx" ON "AuditLog"("notionPageId");

-- CreateIndex
CREATE INDEX "AuditLog_actorSource_idx" ON "AuditLog"("actorSource");

-- CreateIndex
CREATE INDEX "AuditLog_createdAt_idx" ON "AuditLog"("createdAt");
