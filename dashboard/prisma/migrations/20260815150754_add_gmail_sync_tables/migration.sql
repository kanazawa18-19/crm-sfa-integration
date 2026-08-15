-- CreateTable
CREATE TABLE "RepGmailConnection" (
    "id" TEXT NOT NULL,
    "repEmail" TEXT NOT NULL,
    "refreshTokenEnc" TEXT NOT NULL,
    "connectedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "lastSyncedAt" TIMESTAMP(3),

    CONSTRAINT "RepGmailConnection_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "EmailLog" (
    "id" TEXT NOT NULL,
    "contactPageId" TEXT NOT NULL,
    "contactEmail" TEXT NOT NULL,
    "repEmail" TEXT NOT NULL,
    "gmailMessageId" TEXT NOT NULL,
    "direction" TEXT NOT NULL,
    "subject" TEXT,
    "snippet" TEXT,
    "sentAt" TIMESTAMP(3) NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "EmailLog_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "RepGmailConnection_repEmail_key" ON "RepGmailConnection"("repEmail");

-- CreateIndex
CREATE UNIQUE INDEX "EmailLog_gmailMessageId_key" ON "EmailLog"("gmailMessageId");

-- CreateIndex
CREATE INDEX "EmailLog_contactPageId_idx" ON "EmailLog"("contactPageId");

-- CreateIndex
CREATE INDEX "EmailLog_contactEmail_idx" ON "EmailLog"("contactEmail");
