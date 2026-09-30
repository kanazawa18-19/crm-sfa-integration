CREATE TABLE "RecordSyncFieldWatermark" (
    "dbKey" TEXT NOT NULL,
    "notionKey" TEXT NOT NULL,
    "propertyName" TEXT NOT NULL,
    "acceptedAt" TIMESTAMPTZ(6) NOT NULL,
    "completedAt" TIMESTAMPTZ(6),
    CONSTRAINT "RecordSyncFieldWatermark_pkey" PRIMARY KEY ("dbKey", "notionKey", "propertyName")
);

-- 切替直前まで旧コードが進める時刻は、配備後の最初のイベントで一度だけ固定する。
