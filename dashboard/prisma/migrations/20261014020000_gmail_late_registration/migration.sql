CREATE TABLE "GmailContactDiscovery" (
  key TEXT PRIMARY KEY, data JSONB NOT NULL DEFAULT '{}', "updatedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);
CREATE TABLE "GmailContactEmail" (
  "pageId" TEXT PRIMARY KEY, email TEXT NOT NULL, generation TEXT NOT NULL,
  "observedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);
CREATE TABLE "GmailLateRegistrationJob" (
  id TEXT PRIMARY KEY, "pageId" TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  data JSONB NOT NULL, "createdAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
  "updatedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);
CREATE INDEX "GmailLateRegistrationJob_state_updatedAt_idx" ON "GmailLateRegistrationJob"(state,"updatedAt");
