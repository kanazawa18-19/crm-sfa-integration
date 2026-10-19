-- 通常のaction_id索引と併用し、固定訂正の旧ページ照会で受付全件を走査しない。
CREATE INDEX "EmailReview_correction_pages_idx"
ON "EmailReview" USING GIN ((data->'correction_hold'->'blocked_action_ids'));
