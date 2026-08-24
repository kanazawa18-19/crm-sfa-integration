-- CreateEnum
CREATE TYPE "RelationReviewStatus" AS ENUM ('pending', 'resolved', 'dismissed');

-- CreateTable
CREATE TABLE "ClientNameIndex" (
    "id" TEXT NOT NULL,
    "notionPageId" TEXT NOT NULL,
    "normalizedName" TEXT NOT NULL,
    "rawName" TEXT NOT NULL,
    "syncedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ClientNameIndex_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "RelationReviewQueue" (
    "id" TEXT NOT NULL,
    "sourceTool" TEXT NOT NULL,
    "sourceRecordId" TEXT NOT NULL,
    "targetDbKey" TEXT NOT NULL,
    "rawValue" TEXT NOT NULL,
    "candidateNotionPageIds" JSONB NOT NULL,
    "candidateRawNames" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    "status" "RelationReviewStatus" NOT NULL DEFAULT 'pending',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "resolvedAt" TIMESTAMP(3),
    "resolvedNotionPageId" TEXT,

    CONSTRAINT "RelationReviewQueue_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "ClientNameIndex_notionPageId_key" ON "ClientNameIndex"("notionPageId");

-- CreateIndex
CREATE INDEX "ClientNameIndex_normalizedName_idx" ON "ClientNameIndex"("normalizedName");

-- CreateIndex
CREATE INDEX "RelationReviewQueue_status_idx" ON "RelationReviewQueue"("status");

-- CreateIndex
CREATE INDEX "RelationReviewQueue_sourceTool_idx" ON "RelationReviewQueue"("sourceTool");

-- CreateIndex
-- 部分ユニークインデックス(status='pendingの行のみ対象)。src/relation_sync/review_queue.pyの
-- enqueue_for_review()がON CONFLICT (...) WHERE status = 'pending' DO NOTHINGの一致対象
-- (arbiter index)として使う。同一(sourceTool, sourceRecordId, targetDbKey, rawValue)の
-- 組み合わせでpending状態の重複INSERTを、SELECT→INSERTの2クエリではなくDB側で原子的に
-- 防止するため(shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-25)。resolved/
-- dismissedになった行は対象外のため、過去に一度レビュー済みの組み合わせが再度曖昧になった
-- 場合は新規に積み直せる。Prismaのスキーマ定義言語(schema.prisma)はフィルタ条件付き
-- (部分)インデックスを表現できないため、このインデックスはPrisma側の@@unique/@@indexとしては
-- 宣言せず、このmigration.sqlにのみ存在する(dashboard/prisma/schema.prismaの
-- RelationReviewQueueモデル直上のコメント参照)。
CREATE UNIQUE INDEX "RelationReviewQueue_pending_dedupe_key"
    ON "RelationReviewQueue" ("sourceTool", "sourceRecordId", "targetDbKey", "rawValue")
    WHERE status = 'pending';
