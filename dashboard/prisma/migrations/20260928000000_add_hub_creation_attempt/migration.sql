-- POST前に予約を確定する。reservedの再送は作成せず本人の確認を待つ。
CREATE TABLE "HubCreationAttempt" (
    "sourceKey" TEXT NOT NULL,
    target TEXT NOT NULL,
    "dbKey" TEXT NOT NULL,
    "identityHash" TEXT,
    state TEXT NOT NULL CHECK (state IN ('blocked','reserved','created')),
    reason TEXT NOT NULL DEFAULT '',
    "externalId" TEXT,
    "createdAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("sourceKey", target)
);
CREATE UNIQUE INDEX "HubCreationAttempt_identity_active" ON "HubCreationAttempt" (target,"dbKey","identityHash")
    WHERE state IN ('reserved','created');
