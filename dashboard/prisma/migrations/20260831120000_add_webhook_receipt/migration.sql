-- Webhookの最終受信時刻と累計件数を記録するテーブル（2026-08-31）。
-- 追加のみ（既存テーブルには触れない）。
CREATE TABLE IF NOT EXISTS "WebhookReceipt" (
    "source" TEXT NOT NULL,
    "lastReceivedAt" TIMESTAMP(3) NOT NULL,
    "receiptCount" BIGINT NOT NULL DEFAULT 0,

    CONSTRAINT "WebhookReceipt_pkey" PRIMARY KEY ("source")
);
