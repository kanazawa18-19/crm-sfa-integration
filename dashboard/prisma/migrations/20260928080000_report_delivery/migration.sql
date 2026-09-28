CREATE TABLE "ReportCollection" (
    "reportDate" DATE PRIMARY KEY,
    cutoff TIMESTAMPTZ NOT NULL,
    phase TEXT NOT NULL DEFAULT 'project',
    cursor JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    "retryCount" INTEGER NOT NULL DEFAULT 0,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "completedAt" TIMESTAMPTZ
);
CREATE TABLE "ReportCollectionPage" (
    "reportDate" DATE NOT NULL REFERENCES "ReportCollection"("reportDate"),
    "dbKey" TEXT NOT NULL,
    "pageId" TEXT NOT NULL,
    page JSONB NOT NULL,
    PRIMARY KEY ("reportDate","dbKey","pageId")
);
CREATE TABLE "ReportDelivery" (
    "reportDate" DATE NOT NULL REFERENCES "ReportCollection"("reportDate"),
    kind TEXT NOT NULL,
    "destinationHash" TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved',
    "bodyHash" TEXT NOT NULL,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "deliveredAt" TIMESTAMPTZ,
    PRIMARY KEY ("reportDate",kind)
);
