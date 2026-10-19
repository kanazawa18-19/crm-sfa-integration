CREATE TABLE "ContactAffiliationJob" (
 id TEXT PRIMARY KEY DEFAULT 'main', enabled BOOLEAN NOT NULL DEFAULT false,
 metrics JSONB NOT NULL DEFAULT '{}', "mailCursor" JSONB NOT NULL DEFAULT '{}', "dryRun" BOOLEAN NOT NULL DEFAULT true, discovery JSONB NOT NULL DEFAULT '{}',
 "lastRunAt" TIMESTAMPTZ, "lastSuccessAt" TIMESTAMPTZ, "lastError" TEXT
);
INSERT INTO "ContactAffiliationJob" (id) VALUES ('main');
CREATE TABLE "ContactAffiliationItem" (
 "notionId" TEXT PRIMARY KEY, email TEXT, state TEXT NOT NULL DEFAULT 'pending',
 decision JSONB NOT NULL DEFAULT '{}', "nextAttemptAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
 "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(), "errorType" TEXT
);
CREATE INDEX "ContactAffiliationItem_due_idx" ON "ContactAffiliationItem" ("nextAttemptAt", "notionId");
CREATE TABLE "ContactAffiliationHistory" (
 id BIGSERIAL PRIMARY KEY, "notionId" TEXT NOT NULL, state TEXT NOT NULL,
 decision JSONB NOT NULL, "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE "ContactAffiliationEvidence" (
 "sourceId" TEXT PRIMARY KEY, email TEXT NOT NULL, data JSONB NOT NULL,
 "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX "ContactAffiliationEvidence_email_idx" ON "ContactAffiliationEvidence" (email);

CREATE INDEX "ContactAffiliationItem_email_idx" ON "ContactAffiliationItem" (email);
