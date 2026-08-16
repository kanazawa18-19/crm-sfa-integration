-- AlterTable
ALTER TABLE "AppSettings" ADD COLUMN     "emailReminderEnabled" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "emailReminderThresholdHours" INTEGER[] DEFAULT ARRAY[]::INTEGER[];

-- CreateTable
CREATE TABLE "EmailReminderLog" (
    "id" TEXT NOT NULL,
    "emailLogId" TEXT NOT NULL,
    "thresholdHours" INTEGER NOT NULL,
    "sentAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "EmailReminderLog_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "EmailReminderLog_emailLogId_thresholdHours_key" ON "EmailReminderLog"("emailLogId", "thresholdHours");

-- AddForeignKey
ALTER TABLE "EmailReminderLog" ADD CONSTRAINT "EmailReminderLog_emailLogId_fkey" FOREIGN KEY ("emailLogId") REFERENCES "EmailLog"("id") ON DELETE CASCADE ON UPDATE CASCADE;
