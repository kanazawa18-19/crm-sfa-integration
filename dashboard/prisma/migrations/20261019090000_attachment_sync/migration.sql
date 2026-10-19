CREATE TABLE "AttachmentSyncState" (
  "id" TEXT PRIMARY KEY,
  "data" JSONB NOT NULL DEFAULT '{}',
  "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE "AttachmentSyncSource" (
  "id" TEXT PRIMARY KEY,
  "data" JSONB NOT NULL,
  "state" TEXT NOT NULL DEFAULT 'pending',
  "assetId" TEXT,
  "reason" TEXT,
  "nextAttemptAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
  "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX "AttachmentSyncSource_pending_idx" ON "AttachmentSyncSource" ("nextAttemptAt", "id");
CREATE TABLE "AttachmentSyncAsset" (
  "id" TEXT PRIMARY KEY,
  "data" JSONB NOT NULL,
  "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
