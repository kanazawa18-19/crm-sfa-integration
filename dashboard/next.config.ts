import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    // Server Actionsのデフォルトbody上限(1MB)だと、lib/avatar.tsのAVATAR_MAX_BYTES
    // (2MB)まで許可しているアイコン画像アップロードがフレームワーク側で413になり、
    // updateOwnAvatarのtry/catchでは捕捉できない(shirokuma-secレビュー指摘、
    // 2026-08-16)。multipart/form-dataのオーバーヘッド分の余裕を見て3mbにする。
    serverActions: {
      bodySizeLimit: "3mb",
    },
  },
};

export default nextConfig;
