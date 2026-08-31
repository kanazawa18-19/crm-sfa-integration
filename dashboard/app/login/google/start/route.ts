import { randomBytes } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import {
  LOGIN_PURPOSE,
  LOGIN_STATE_COOKIE,
  LOGIN_STATE_COOKIE_PATH,
  buildLoginAuthUrl,
} from "@/lib/googleLoginOauth";

// 「Googleでログイン」の1歩目(2026-08-31)。**ログイン前に叩かれるので
// proxy.tsのPUBLIC_PATHSに入っている。**
//
// Gmail/Drive連携フロー(app/gmail/oauth/start・app/google/oauth/start)とは
// stateのpurposeで区別する。コールバックは同じ /gmail/oauth/callback を使う
// (Google Cloud Consoleに登録済みのredirect_uriがそれ1本しかないため。
// 詳細は lib/googleLoginOauth.ts と app/google/oauth/README.md)。
//
// stateのnonceは連携フローとは**別のcookie**に入れる。同じcookie名を使い回すと、
// Gmail連携の途中でログインし直したときに片方のnonceがもう片方を上書きし、
// 進行中のフローが無言で invalid_state になるため。
// 定数は lib/googleLoginOauth.ts に置いている。route.ts の export surface は
// Next.jsが規定しており、独自exportはビルド・typegenの条件で壊れうるため
// (2026-08-31、ChatGPTのレビュー指摘)。

export async function GET(_request: NextRequest) {
  const nonce = randomBytes(16).toString("hex");
  const response = NextResponse.redirect(buildLoginAuthUrl(`${nonce}.${LOGIN_PURPOSE}`));
  response.cookies.set(LOGIN_STATE_COOKIE, nonce, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    maxAge: 600,
    // コールバックが /gmail/oauth/callback なので、そこから読める範囲に置く。
    path: LOGIN_STATE_COOKIE_PATH,
  });
  return response;
}
