-- シートの行を作れなかったレコードの再試行キュー（2026-09-07）。追加のみ。
--
-- Webhookは「行を作れなかった」ときも2xxを返す（Notionページとマッピングは既にできて
-- いるため、500でリトライさせると重複作成の危険な経路を叩き直すことになる）。
-- その結果、そのレコードが二度と編集されなければシートには永久に現れなかった。
-- ここに積んで、日次のcronが作り直す。
CREATE TABLE IF NOT EXISTS "SpreadsheetOutbox" (
    "dbKey" TEXT NOT NULL,
    "notionKey" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "reason" TEXT NOT NULL,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "nextAttemptAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "lastError" TEXT,
    -- どう決着したか（created / already_present / not_applicable / gave_up /
    -- recovered_manually）。「行を作れた」と「そもそも要らなかった」を status だけでは
    -- 区別できず、outboxが実際に効いているかを後から数えられないため分けて持つ。
    "resolution" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "resolvedAt" TIMESTAMP(3),

    -- 1レコードにつき1行。同じレコードで何度失敗しても行は増えない。
    CONSTRAINT "SpreadsheetOutbox_pkey" PRIMARY KEY ("dbKey", "notionKey")
);

-- 取り出しは「pending のうち再試行時刻が来たもの」。この順で引く。
CREATE INDEX IF NOT EXISTS "SpreadsheetOutbox_status_nextAttemptAt_idx"
    ON "SpreadsheetOutbox"("status", "nextAttemptAt");

-- 片付け（resolvedAt が古い done を消す）と、滞留の古さを数えるのに使う。
CREATE INDEX IF NOT EXISTS "SpreadsheetOutbox_resolvedAt_idx"
    ON "SpreadsheetOutbox"("resolvedAt");
