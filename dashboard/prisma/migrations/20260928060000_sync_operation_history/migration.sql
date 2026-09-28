CREATE TABLE "SyncOperationHistory" (
    id TEXT PRIMARY KEY,
    "actorId" TEXT NOT NULL,
    kind TEXT NOT NULL,
    "subjectId" TEXT NOT NULL,
    "beforeState" JSONB NOT NULL,
    "afterState" JSONB NOT NULL,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX "SyncOperationHistory_subject_idx" ON "SyncOperationHistory" ("subjectId", "createdAt");
