-- 外部書込前に受理時刻を保存し、失敗後も古いイベントを通さない。
CREATE TABLE "RecordSyncWatermark" (
    "dbKey" TEXT NOT NULL,
    "notionKey" TEXT NOT NULL,
    "acceptedAt" TIMESTAMPTZ(6) NOT NULL,
    "completedAt" TIMESTAMPTZ(6),
    PRIMARY KEY ("dbKey", "notionKey")
);
