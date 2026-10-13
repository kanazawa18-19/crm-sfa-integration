-- メール本文・宛先を持たず、観測範囲とID別結果を保持する。
CREATE TABLE "GmailIngestObservation" (
    "eventId" TEXT PRIMARY KEY,
    "scanId" TEXT NOT NULL,
    "phase" TEXT NOT NULL,
    "requestHash" TEXT NOT NULL,
    "evidence" JSONB NOT NULL,
    "response" JSONB NOT NULL,
    "receivedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);
CREATE INDEX "GmailIngestObservation_scanId_idx" ON "GmailIngestObservation"("scanId");
CREATE INDEX "GmailIngestObservation_receivedAt_idx" ON "GmailIngestObservation"("receivedAt");
