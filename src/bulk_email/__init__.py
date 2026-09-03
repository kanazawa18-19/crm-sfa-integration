"""一斉配信（営業メールのまとめ送り）の中身（2026-09-03）。

設計の経緯と、まだ決まっていないことは`docs/bulk_email_design_note.md`にある。

■ この時点で入っているのは「プレビューまで」

段階リリースの①だけを実装している。**1通も送らない。**

```
   ① プレビューのみ    宛先一覧と差し込み後の本文を画面に出す   ← ここまで
   ② 自分宛だけ        本人のアドレスにだけ実送信
   ③ 少数（5件以内）   実際の顧客へ
   ④ 本番             件数制限を外す
```

②以降は送信経路（Gmail APIに`gmail.send`を足すか）が決まらないと書けない。
`gmail.readonly`は読み取り専用のスコープで、送信はできない。

■ 層の分け方

```
   src/api/routes/bulk_email.py   HTTP。入力の検証とdictへの変換だけ
            │
            ▼
   src/api/bulk_email_service.py  ユースケース。Notionから連絡先を取り、
            │                     配信停止をPostgresから引いて、preview へ渡す
            ▼
   src/bulk_email/preview.py      ★ここから下は外部I/Oを一切しない純粋関数
     ├── audience.py    誰に送れて、誰を外すか
     ├── template.py    差し込み
     ├── compliance.py  特定電子メール法の表示（会社名・住所・配信停止）
     └── unsubscribe.py 配信停止リンクの署名
            ▲
            │
   src/bulk_email/db.py           Postgres（配信停止フラグの読み取り）
```

`preview.build_preview()`はDBもNotionも起動せずにテストできる。ここが本体で、
上下は薄いままにしておくこと。

■ 状態（BulkCampaign等）のテーブルはまだ作っていない

設計メモには`BulkCampaign`/`BulkCampaignRecipient`を書いたが、**送信が無い今は
書き込む中身が無い**ため意図的に作っていない。使われないテーブルを先に作ると、
②で実際に必要な形が分かったときに作り直すことになる。今あるのは配信停止
（`ContactMailPreference`）だけで、これは送信の有無に関わらず今すぐ要る。
"""
