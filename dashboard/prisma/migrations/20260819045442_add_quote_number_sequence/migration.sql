-- CreateTable
CREATE TABLE "QuoteNumberSequence" (
    "datePrefix" TEXT NOT NULL,
    "lastSeq" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "QuoteNumberSequence_pkey" PRIMARY KEY ("datePrefix")
);
