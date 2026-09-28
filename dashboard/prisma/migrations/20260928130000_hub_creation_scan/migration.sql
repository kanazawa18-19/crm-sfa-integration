-- 重複照合の途中位置だけを保持する。外部作成の予約とは分ける。
CREATE TABLE "HubCreationScan" (
    "sourceKey" TEXT PRIMARY KEY,
    "sourceId" TEXT NOT NULL,
    "dbKey" TEXT NOT NULL,
    "inputHash" TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','held','done')),
    checkpoint JSONB NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    "nextAttemptAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);
CREATE INDEX "HubCreationScan_due" ON "HubCreationScan" ("nextAttemptAt") WHERE state='pending';
