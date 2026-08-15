"""Gmail連携移管(2026-08-16)。web-engagement-tool(MA)側が個別に行っていたGmail
OAuth・ポーリングをこちら(crm-sfa-integration)側に一本化する。

- token_crypto.py: dashboard(Next.js)側のlib/tokenCrypto.tsと同じAES-256-GCM実装
  (同じTOKEN_ENCRYPTION_KEYを共有し、暗号化したリフレッシュトークンを言語を跨いで
  読み書きできるようにする)
- db.py: RepGmailConnection/EmailLogテーブル(Neon Postgres、スキーマ管理はdashboard
  側のPrismaに一本化)へのpsycopg直接アクセス
- matcher.py: メールアドレスからNotion連絡先DBのページを1件に絞り込む
- gmail_client.py: OAuthリフレッシュトークンでGmail APIを直接叩く(REST、
  google-api-python-clientは使わずrequestsで最小限に実装)
- sync.py: 上記を組み合わせた同期本体(cron等から呼ばれる想定)
"""
