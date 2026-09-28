# 通知記録と実配送の照合

2026-09-28、Slack読み取り連携で保存済みメッセージを取得。本番Postgresは読み取り専用トランザクションで照合し、送信済み印の変更・再送は実施していない。

|経路|確認した事実|残る制約|
|---|---|---|
|日次ダイジェスト／operations_dm|中優先度7行は全て送信済み印あり。9/15・9/22の2行は相手・件名・送信時刻5秒以内でSlack実物と一致|9/1・9/2・9/3・9/9・9/12の5行は今回の検索で対応メッセージを確認できず。未送達確定とは扱わない。9/12と9/15は同じ相手・件名のため、本文だけでは同一通知と判定しない|
|未返信リマインド|9/27の3行と9/28 09:21の1行は相手・件名・送信時刻5秒以内でSlack実物と一致|9/26以前や他担当者の全通知を保証するものではない|
|管理者への新規作成通知|9/28 01:23・08:43の取引先作成通知を本人DMから取得|他管理者の受信は未確認|
|必須不足通知|9/14のmissing_required_properties通知を本人DMから取得|現在版で意図的な不足試験は行っていない|
|週次点検／日次監視|9/28の実メッセージを取得|詳細はscheduled-checks.md|

照合済み実物：[日次9/22](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790049635445919)、[日次9/15](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1789446453467119)、[未返信9/28](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790554904851339)、[未返信9/27その1](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790516183197319)、[その2](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790516183003029)、[その3](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790471295754429)、[必須不足](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1789356167376179)。顧客のメールアドレス・件名は本証跡へ転記しない。

非公開証跡はgit除外済み `migration_output/remaining-20260928/notification-{db,slack,correlation}-private.json`。検索で見つからない5行は印を一括解除せず保持する。
