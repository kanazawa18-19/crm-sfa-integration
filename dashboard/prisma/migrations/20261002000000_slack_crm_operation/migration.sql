CREATE TABLE "SlackCrmOperation" (
    "id" TEXT NOT NULL,
    "actorId" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "targetId" TEXT NOT NULL,
    "expected" JSONB NOT NULL,
    "changes" JSONB NOT NULL,
    "state" TEXT NOT NULL DEFAULT 'queued',
    "resultPageId" TEXT,
    "errorCode" TEXT,
    "createdAt" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "startedAt" TIMESTAMPTZ(6),
    "processAfter" TIMESTAMPTZ(6),
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "finishedAt" TIMESTAMPTZ(6),
    "notifyClaimedAt" TIMESTAMPTZ(6),
    "notifiedAt" TIMESTAMPTZ(6),
    "notifyAfter" TIMESTAMPTZ(6),
    "notifyAttempts" INTEGER NOT NULL DEFAULT 0,
    "syncState" TEXT,
    "syncClaimedAt" TIMESTAMPTZ(6),
    "syncAfter" TIMESTAMPTZ(6),
    "syncAttempts" INTEGER NOT NULL DEFAULT 0,
    "syncError" TEXT,
    CONSTRAINT "SlackCrmOperation_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "SlackCrmOperation_state_createdAt_idx"
    ON "SlackCrmOperation"("state", "createdAt");
CREATE INDEX "SlackCrmOperation_actorId_createdAt_idx"
    ON "SlackCrmOperation"("actorId", "createdAt");
