CREATE TABLE "SyncFieldReview" (
  "id" TEXT PRIMARY KEY,
  "dbKey" TEXT NOT NULL,
  "notionKey" TEXT NOT NULL,
  "propertyName" TEXT NOT NULL,
  "sourceTool" TEXT NOT NULL,
  "eventAt" TIMESTAMPTZ NOT NULL,
  "snapshot" JSONB NOT NULL,
  "snapshotHash" TEXT NOT NULL,
  "state" TEXT NOT NULL DEFAULT 'pending',
  "revision" INTEGER NOT NULL DEFAULT 0,
  "progress" JSONB NOT NULL DEFAULT '{}',
  "lastError" TEXT,
  "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE ("dbKey", "notionKey", "propertyName", "sourceTool", "eventAt")
);
CREATE INDEX "SyncFieldReview_state_idx" ON "SyncFieldReview" ("state", "createdAt");
CREATE INDEX "SyncFieldReview_record_idx" ON "SyncFieldReview" ("dbKey", "notionKey", "propertyName");
CREATE TABLE "SyncFieldReviewHistory" (
  "id" BIGSERIAL PRIMARY KEY,
  "reviewId" TEXT NOT NULL REFERENCES "SyncFieldReview"("id"),
  "actorId" TEXT NOT NULL,
  "action" TEXT NOT NULL,
  "snapshot" JSONB,
  "revision" INTEGER NOT NULL,
  "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- kintoneの全項目通知で、初めからの空欄と今回の削除を区別する。
CREATE TABLE "SyncSourceObservation" (
  "dbKey" TEXT NOT NULL,
  "notionKey" TEXT NOT NULL,
  "sourceTool" TEXT NOT NULL,
  "values" JSONB NOT NULL DEFAULT '{}',
  "eventAt" TIMESTAMPTZ NOT NULL,
  PRIMARY KEY ("dbKey", "notionKey", "sourceTool")
);
