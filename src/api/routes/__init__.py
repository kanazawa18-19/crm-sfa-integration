"""FastAPIのルーター群（2026-08-28にsrc/api/app.pyから分割）。

分割の狙いは「どのパスがどこにあるか」を探しやすくすること。`app.py`は992行あり、
Webhook受信・定期実行cron・dashboard向け読み取りAPIが1ファイルに同居していた。

**パスは外部システム側に登録された宛先**（vercel.jsonのcron、kintone/Zoho/Notion/
Gmail Pub/Sub/Slack/MAのWebhook URL）であり、分割で1つでも欠けたり変わったりすると
本番の同期が静かに止まる。`tests/api/test_route_registry.py`が公開ルートの集合そのものを
固定しているので、構成を変えてもそこが変わらないことを必ず確認すること。
"""
