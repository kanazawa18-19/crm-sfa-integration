CREATE TABLE "GmailDeferredMessage" (
    "repEmail" TEXT NOT NULL,
    "gmailMessageId" TEXT NOT NULL,
    "contactPageId" TEXT NOT NULL,
    "contactEmail" TEXT NOT NULL,
    "message" JSONB NOT NULL,
    "internalDomains" JSONB NOT NULL,
    "state" TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "lastErrorType" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "nextAttemptAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "completedAt" TIMESTAMPTZ,
    PRIMARY KEY ("repEmail", "gmailMessageId")
);
CREATE INDEX "GmailDeferredMessage_due_idx" ON "GmailDeferredMessage" (state, "nextAttemptAt");
