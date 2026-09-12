-- 既存の名前同期と共存する追加列。未取り込みを空の照合結果と混同しない。
ALTER TABLE "ClientNameIndex"
    ADD COLUMN "normalizedAddress" TEXT,
    ADD COLUMN "normalizedPhone" TEXT,
    ADD COLUMN "identitySyncedAt" TIMESTAMP(3);
CREATE INDEX "ClientNameIndex_normalizedAddress_idx" ON "ClientNameIndex"("normalizedAddress");
CREATE INDEX "ClientNameIndex_normalizedPhone_idx" ON "ClientNameIndex"("normalizedPhone");
