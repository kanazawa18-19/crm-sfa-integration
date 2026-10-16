CREATE TABLE "EmailReview" (
  "id" TEXT PRIMARY KEY,
  "data" JSONB NOT NULL,
  "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX "EmailReview_state_idx" ON "EmailReview" ((data->>'state'), "updatedAt");
CREATE UNIQUE INDEX "EmailReview_canonical_idx" ON "EmailReview" ((data->>'canonical_key')) WHERE data->>'canonical_key' IS NOT NULL;
CREATE UNIQUE INDEX "EmailReview_action_idx" ON "EmailReview" ((data->>'action_id')) WHERE data->>'action_id' IS NOT NULL;
CREATE INDEX "ClientNameIndex_email_normalized_idx" ON "ClientNameIndex" (lower("normalizedName"));
CREATE TABLE "EmailReviewOwner" (
  "reviewId" TEXT NOT NULL REFERENCES "EmailReview"("id"),
  "ownerEmail" TEXT NOT NULL,
  "gmailMessageId" TEXT NOT NULL,
  "threadId" TEXT,
  PRIMARY KEY ("ownerEmail", "gmailMessageId")
);
CREATE INDEX "EmailReviewOwner_review_idx" ON "EmailReviewOwner" ("reviewId", "ownerEmail");
CREATE INDEX "EmailReviewOwner_thread_idx" ON "EmailReviewOwner" ("ownerEmail", "threadId");
CREATE TABLE "EmailReviewHistory" (
  "id" BIGSERIAL PRIMARY KEY,
  "reviewId" TEXT NOT NULL REFERENCES "EmailReview"("id"),
  "data" JSONB NOT NULL,
  "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);
