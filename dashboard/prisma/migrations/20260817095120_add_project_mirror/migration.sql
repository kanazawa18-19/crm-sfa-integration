-- CreateTable
CREATE TABLE "ProjectMirror" (
    "id" TEXT NOT NULL,
    "notionPageId" TEXT NOT NULL,
    "data" JSONB NOT NULL,
    "lastEditedAt" TIMESTAMP(3),
    "syncedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ProjectMirror_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "ProjectMirror_notionPageId_key" ON "ProjectMirror"("notionPageId");

-- CreateIndex
CREATE INDEX "ProjectMirror_syncedAt_idx" ON "ProjectMirror"("syncedAt");
