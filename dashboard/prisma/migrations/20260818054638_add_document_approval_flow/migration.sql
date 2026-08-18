-- CreateTable
CREATE TABLE "RepDriveConnection" (
    "id" TEXT NOT NULL,
    "repEmail" TEXT NOT NULL,
    "refreshTokenEnc" TEXT NOT NULL,
    "connectedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "RepDriveConnection_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "DocumentApprover" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "title" TEXT,
    "active" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "DocumentApprover_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "DocumentApproval" (
    "id" TEXT NOT NULL,
    "notionProjectId" TEXT NOT NULL,
    "category" TEXT NOT NULL,
    "driveFileId" TEXT NOT NULL,
    "driveApprovalId" TEXT NOT NULL,
    "approverEmail" TEXT NOT NULL,
    "requestedByEmail" TEXT NOT NULL,
    "status" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "resolvedAt" TIMESTAMP(3),

    CONSTRAINT "DocumentApproval_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "RepDriveConnection_repEmail_key" ON "RepDriveConnection"("repEmail");

-- CreateIndex
CREATE UNIQUE INDEX "DocumentApprover_email_key" ON "DocumentApprover"("email");

-- CreateIndex
CREATE INDEX "DocumentApproval_status_idx" ON "DocumentApproval"("status");

-- CreateIndex
CREATE INDEX "DocumentApproval_notionProjectId_idx" ON "DocumentApproval"("notionProjectId");
