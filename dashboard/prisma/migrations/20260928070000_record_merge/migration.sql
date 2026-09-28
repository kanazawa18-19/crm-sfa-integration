CREATE TABLE "RecordMergeJob" (
    id TEXT PRIMARY KEY,
    "dbKey" TEXT NOT NULL,
    "sourceId" TEXT NOT NULL,
    "targetId" TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft',
    snapshot JSONB NOT NULL,
    steps JSONB NOT NULL,
    "planHash" TEXT NOT NULL,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    "actorId" TEXT NOT NULL,
    error TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ("sourceId" <> "targetId"),
    CHECK (state IN ('draft','approved','running','held','dismissed','done','abandoned'))
);
CREATE INDEX "RecordMergeJob_state_idx" ON "RecordMergeJob" (state, "updatedAt");
CREATE UNIQUE INDEX "RecordMergeJob_active_source_idx" ON "RecordMergeJob" ("dbKey", "sourceId")
    WHERE state NOT IN ('dismissed','done','abandoned');
CREATE TABLE "RecordMergeAlias" (
    "dbKey" TEXT NOT NULL,
    tool TEXT NOT NULL,
    "oldId" TEXT NOT NULL,
    "targetId" TEXT NOT NULL,
    "operationId" TEXT NOT NULL REFERENCES "RecordMergeJob"(id),
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY ("dbKey", tool, "oldId")
);
CREATE INDEX "RecordMergeAlias_target_idx" ON "RecordMergeAlias" ("dbKey", "targetId");
CREATE TABLE "RecordMergeAliasEvent" (
    id TEXT PRIMARY KEY,
    "dbKey" TEXT NOT NULL,
    "sourceTool" TEXT NOT NULL,
    "oldId" TEXT NOT NULL,
    "targetId" TEXT NOT NULL,
    "eventAt" TIMESTAMPTZ NOT NULL,
    properties JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "resolvedAt" TIMESTAMPTZ
);
CREATE INDEX "RecordMergeAliasEvent_pending_idx" ON "RecordMergeAliasEvent" (state,"createdAt");
CREATE TABLE "RelationReviewDecision" (
    "reviewId" TEXT PRIMARY KEY,
    snapshot JSONB NOT NULL,
    "snapshotHash" TEXT NOT NULL,
    "actorId" TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'approved',
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE "RelationResolution" (
    "sourceTool" TEXT NOT NULL,
    "sourceRecordId" TEXT NOT NULL,
    "targetDbKey" TEXT NOT NULL,
    "rawValue" TEXT NOT NULL,
    "targetNotionId" TEXT NOT NULL,
    "reviewId" TEXT NOT NULL REFERENCES "RelationReviewDecision"("reviewId"),
    PRIMARY KEY ("sourceTool","sourceRecordId","targetDbKey","rawValue")
);
CREATE TABLE "RecordCreationCandidate" (
    id TEXT PRIMARY KEY,
    "dbKey" TEXT NOT NULL,
    "sourceId" TEXT NOT NULL,
    tool TEXT NOT NULL,
    "externalId" TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    "snapshotHash" TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    "importedNotionId" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX "RecordCreationCandidate_pending_idx" ON "RecordCreationCandidate" (state,"updatedAt");
