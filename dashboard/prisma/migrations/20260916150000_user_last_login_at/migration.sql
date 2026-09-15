-- ログイン成立日時。ログインを Google だけにしたため、「招待を承諾済み」の印を passwordHash に
-- 頼れなくなった（web-engagement-tool と同じ変更）。NULL 許可の追加列だけで、既存行には触れない。
ALTER TABLE "User" ADD COLUMN "lastLoginAt" TIMESTAMP(3);
