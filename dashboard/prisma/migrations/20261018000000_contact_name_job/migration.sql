CREATE TABLE "ContactNameJob" (
    id TEXT PRIMARY KEY,
    "planHash" TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT false,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "lastRunAt" TIMESTAMPTZ,
    "lastSuccessAt" TIMESTAMPTZ
);
CREATE TABLE "ContactNameJobItem" (
    "jobId" TEXT NOT NULL REFERENCES "ContactNameJob"(id),
    "notionId" TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    shard INTEGER NOT NULL CHECK (shard BETWEEN 0 AND 3),
    plan JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','retry','verified','held','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    "nextAttemptAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "errorType" TEXT,
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY ("jobId", "notionId"),
    UNIQUE ("jobId", ordinal)
);
CREATE INDEX "ContactNameJobItem_pending_idx" ON "ContactNameJobItem" ("jobId", shard, "nextAttemptAt", ordinal)
    WHERE state IN ('pending', 'retry');
