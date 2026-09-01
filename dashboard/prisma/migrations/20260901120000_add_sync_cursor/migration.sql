-- 長時間かかる全件同期の進捗（2026-09-01）。追加のみ。
CREATE TABLE IF NOT EXISTS "SyncCursor" (
    "name" TEXT NOT NULL,
    "watermark" TEXT,
    "passStartedAt" TIMESTAMP(3) NOT NULL,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "SyncCursor_pkey" PRIMARY KEY ("name")
);
