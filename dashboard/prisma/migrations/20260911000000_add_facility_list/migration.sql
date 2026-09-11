-- リスト作成マシーン(2026-09-11)。楽天トラベルの公開ページから集めた宿泊施設の母集団と、
-- チェックイン機の判定結果、リスト作成の実行履歴。

-- CreateEnum
CREATE TYPE "FacilityCategory" AS ENUM ('hotel', 'ryokan', 'pension', 'villa', 'other', 'unknown');

-- CreateEnum
CREATE TYPE "FacilityCategoryConfidence" AS ENUM ('high', 'medium', 'low', 'none');

-- CreateEnum
CREATE TYPE "CustomPageStatus" AS ENUM ('published', 'not_published', 'unknown');

-- CreateEnum
CREATE TYPE "CheckInMachineStatus" AS ENUM ('yes', 'no', 'unknown');

-- CreateEnum
CREATE TYPE "CheckInMachineSource" AS ENUM ('facility_page', 'web_search', 'crm', 'none');

-- CreateTable
CREATE TABLE "RakutenFacility" (
    "id" TEXT NOT NULL,
    "hotelNo" INTEGER NOT NULL,
    "name" TEXT NOT NULL,
    "postalCode" TEXT,
    "prefecture" TEXT,
    "city" TEXT,
    "address" TEXT,
    "roomCount" INTEGER,
    "reviewAverage" DECIMAL(3,2),
    "reviewCount" INTEGER,
    "photoCount" INTEGER,
    "minCharge" INTEGER,
    "category" "FacilityCategory" NOT NULL DEFAULT 'unknown',
    "categoryConfidence" "FacilityCategoryConfidence" NOT NULL DEFAULT 'none',
    "hasOnsen" BOOLEAN NOT NULL DEFAULT false,
    "customPageStatus" "CustomPageStatus" NOT NULL DEFAULT 'unknown',
    "customPageCount" INTEGER NOT NULL DEFAULT 0,
    "facilities" JSONB,
    "roomFacilities" JSONB,
    "isListed" BOOLEAN NOT NULL DEFAULT true,
    "fetchedAt" TIMESTAMP(3),
    "parseWarning" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "RakutenFacility_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "FacilityCheckInMachine" (
    "hotelNo" INTEGER NOT NULL,
    "status" "CheckInMachineStatus" NOT NULL DEFAULT 'unknown',
    "source" "CheckInMachineSource" NOT NULL DEFAULT 'none',
    "evidence" TEXT,
    "evidenceUrl" TEXT,
    "checkedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "FacilityCheckInMachine_pkey" PRIMARY KEY ("hotelNo")
);

-- CreateTable
CREATE TABLE "FacilityListRun" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "criteria" JSONB NOT NULL,
    "totalCount" INTEGER NOT NULL DEFAULT 0,
    "matchedCount" INTEGER NOT NULL DEFAULT 0,
    "newCount" INTEGER NOT NULL DEFAULT 0,
    "ambiguousCount" INTEGER NOT NULL DEFAULT 0,
    "outputUrl" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "FacilityListRun_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "RakutenFacility_hotelNo_key" ON "RakutenFacility"("hotelNo");

-- CreateIndex
CREATE INDEX "RakutenFacility_prefecture_roomCount_idx" ON "RakutenFacility"("prefecture", "roomCount");

-- CreateIndex
CREATE INDEX "RakutenFacility_customPageStatus_idx" ON "RakutenFacility"("customPageStatus");

-- CreateIndex
CREATE INDEX "RakutenFacility_reviewAverage_idx" ON "RakutenFacility"("reviewAverage");

-- CreateIndex
CREATE INDEX "RakutenFacility_fetchedAt_idx" ON "RakutenFacility"("fetchedAt");

-- CreateIndex
CREATE INDEX "FacilityCheckInMachine_status_idx" ON "FacilityCheckInMachine"("status");

-- CreateIndex
CREATE INDEX "FacilityListRun_userId_createdAt_idx" ON "FacilityListRun"("userId", "createdAt");

-- AddForeignKey
ALTER TABLE "FacilityCheckInMachine" ADD CONSTRAINT "FacilityCheckInMachine_hotelNo_fkey" FOREIGN KEY ("hotelNo") REFERENCES "RakutenFacility"("hotelNo") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "FacilityListRun" ADD CONSTRAINT "FacilityListRun_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
