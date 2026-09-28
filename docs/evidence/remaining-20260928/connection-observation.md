# 接続・要求の読み取り観測

2026-09-28。負荷は発生させず、既存稼働の接続数のみを観測した。期限切れ容量試験の再開ではない。

|観測|実測|限界|
|---|---|---|
|PostgreSQL|13:46:07〜13:56:24 JST、20標本、接続4〜6、設定上限112、active最大0、idle in transaction最大0|観測自身の接続を除外。瞬間的な最大負荷や許容容量は証明しない|
|Vercel|取得1000行、要求IDで重複除外50件、全HTTP200。gmail-push46 / notion3 / project-product-links1|CLIのページ重複があり、24時間全量ではない|

私有証跡: `migration_output/remaining-20260928/db-observed-connections.json`、`vercel-request-observation.json`。

Cloud Runの最新観測はgcloudの本人再認証待ち。上記をもって容量試験合格・費用確定とはしない。
