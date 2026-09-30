CREATE TABLE "GmailIngestEffect" (
    "gmailMessageId" TEXT NOT NULL,
    "notionDone" BOOLEAN NOT NULL DEFAULT false,
    "incidentAttempted" BOOLEAN NOT NULL DEFAULT false,
    "engagementAttempted" BOOLEAN NOT NULL DEFAULT false,
    "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "GmailIngestEffect_pkey" PRIMARY KEY ("gmailMessageId")
);
