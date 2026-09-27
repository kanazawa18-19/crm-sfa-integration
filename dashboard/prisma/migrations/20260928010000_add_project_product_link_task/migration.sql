-- 業務値は保存しない。案件評価と、変更前に確定した配送義務を分離する。
CREATE TABLE "ProjectProductLinkTask" (
    "projectId" TEXT PRIMARY KEY,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "nextAttemptAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "lastError" TEXT,
    "errorDbKey" TEXT,
    "errorNotionId" TEXT,
    "evaluationHeld" BOOLEAN NOT NULL DEFAULT FALSE,
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX "ProjectProductLinkTask_nextAttemptAt_idx" ON "ProjectProductLinkTask" ("nextAttemptAt");
CREATE TABLE "ProjectProductLinkPair" (
    "projectId" TEXT NOT NULL REFERENCES "ProjectProductLinkTask"("projectId") ON DELETE CASCADE ON UPDATE CASCADE,
    "productId" TEXT NOT NULL,
    "clientId" TEXT NOT NULL,
    "state" TEXT NOT NULL DEFAULT 'pending',
    "lastError" TEXT,
    "errorDbKey" TEXT,
    "errorNotionId" TEXT,
    PRIMARY KEY ("projectId", "productId", "clientId")
);
CREATE TABLE "ProjectProductLinkDelivery" (
    "projectId" TEXT NOT NULL REFERENCES "ProjectProductLinkTask"("projectId") ON DELETE CASCADE ON UPDATE CASCADE,
    "dbKey" TEXT NOT NULL,
    "notionId" TEXT NOT NULL,
    "state" TEXT NOT NULL DEFAULT 'pending',
    "lastError" TEXT,
    "expectedIds" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    "verificationAttempts" INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY ("projectId", "dbKey", "notionId")
);
