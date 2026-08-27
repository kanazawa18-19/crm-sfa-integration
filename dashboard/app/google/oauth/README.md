# このディレクトリに `callback/` が無い理由

Gmail+Driveをまとめて連携するこのフロー（`start/route.ts`）のOAuthコールバックは、
`app/google/oauth/callback/` ではなく既存の **`app/gmail/oauth/callback/route.ts`** です。

新しいコールバックルートをここに作ると、Google Cloud Console側でそのURLを
redirect URIとして追加登録する必要があります。登録漏れがあると本番で
`redirect_uri_mismatch` になり、既存のGmail単体フローに影響を与えずに気づきにくいため、
このフローは意図的に新規URIを作らず、既に登録済みの `/gmail/oauth/callback` を
そのまま再利用しています（`state` パラメータに `<nonce>.google_all` という形式で
「まとめて連携フロー由来」の印を付け、コールバック側でそれを見て分岐しています）。

詳細は [`docs/google_oauth_note.md`](../../../../docs/google_oauth_note.md) を参照してください。

302で転送するだけの `callback/route.ts` をここに置くことは意図的に避けています。
フローが1本増えるだけで、redirect_uri自体は変わらないため意味がなく、むしろ
「どちらが本物のコールバックか」を分かりにくくするためです。
