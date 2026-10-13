-- 問い合わせ入口で保存した差分だけを再配送できるよう保持する。
CREATE TABLE "InquiryHandoff" (
 "sourceKey" TEXT PRIMARY KEY,
 "pageId" TEXT,
 "inputPayload" JSONB NOT NULL DEFAULT '{}',
 properties JSONB NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'pending',
 "zohoHoldReason" TEXT,
 reason TEXT NOT NULL DEFAULT '',
 "createdAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
 "updatedAt" TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);
CREATE INDEX "InquiryHandoff_state_idx" ON "InquiryHandoff"(state);
