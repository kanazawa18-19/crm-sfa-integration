# Googleスプレッドシートのタブ構成設計メモ（T-02）

`09_開発ロードマップ` T-02「Googleスプレッドシート（全5タブ＋同期ログタブ）のテンプレート作成」の
実装にあたり、仕様書に内訳の明記が無いタブ構成を決定した経緯と理由を記録する。
実装は `gas/tabDefinitions.js`（タブ・ヘッダー定義）、`gas/setupTemplate.js`（自動セットアップ）。

## 仕様書上の曖昧さ

- 09節ロードマップの表記は「全5タブ＋同期ログタブ」。
- 一方、02節DB構成一覧のNotion側は6DB（取引先マスター／チェーン／連絡先／案件管理／
  サービス・商品／アクション管理）。02節の注記「既存Notionに存在しなかった『連絡先』
  『サービス・商品』は独立DBとして新設する」から、6DB化はv2.0での変更であり、
  「全5タブ」という09節の数字はDB分割前（4〜5DB構成だった頃）の記述が更新されずに
  残った可能性が高い。
- さらに06節では「スプレッドシート分析タブ／Notionダッシュボードでグラフ化」
  （段階別受注率等）と「スプレッドシートのクロスセル対象リスト」という、業務DBに
  対応しない追加タブへの言及もある。

## 決定：業務タブは6DB全部、+分析タブ+クロスセル対象タブ+同期ログタブ ＝ 計9タブ

以下の理由により、「全5タブ」という09節の数字よりも、既存実装（本タスク着手前に
実装済みの同期エンジン側コード）との整合性を優先し、6DB全部にタブを割り当てることを
決定した。

1. **既にコードが6DB全部のスプレッドシート連携を前提にしている。**
   `src/db_schema/*.py` の6スキーマ全てが `spreadsheet_sheet_name`
   （例: `contact.py` → `"連絡先"`、`product.py` → `"サービス・商品"`）を持っており、
   `src/sync_engine/webhook_handlers/spreadsheet_webhook.py` の
   `_default_sheet_to_db_key()` はこの6件全てからシート名→DBキーの逆引き表を作る。
   ここで5DBしかタブを用意しないと、残り1DBの `spreadsheet_sheet_name` が
   デッドコード化し、そのDBだけスプレッドシートでの閲覧・編集・Any-to-Any同期が
   不可能になる。これは01節「Any-to-Any 相互同期」（4ツールいずれで編集しても
   他の全ツールへ反映）という基本要件と矛盾する。
2. **6DBのうちどれか1つを意図的に除外する合理的な根拠が仕様書に無い。**
   6DBはいずれも営業現場が日常的に参照・更新する対象（取引先・チェーン・連絡先・
   案件・サービス・アクション）であり、特定の1DBだけスプレッドシートでの閲覧を
   不要とする業務的な理由は読み取れない。
3. **06節の「分析タブ」「クロスセル対象リスト」は既存6DBタブと別枠の出力先として
   明記されている。** これらを業務タブの代わりに数えて5に合わせる（業務タブを
   減らして分析系タブで穴埋めする）と、業務タブが不足する上記1の問題が残るため
   採用しなかった。

結果として次の9タブ構成とした（`gas/tabDefinitions.js` の `ALL_TABS` に対応）。

| # | タブ名 | 種別 | 対応するDB/データ |
|---|---|---|---|
| 1 | 取引先マスター | 業務（Any-to-Any同期対象） | ① 取引先マスターDB |
| 2 | チェーン | 業務（Any-to-Any同期対象） | ② チェーンDB |
| 3 | 連絡先 | 業務（Any-to-Any同期対象） | ③ 連絡先DB |
| 4 | 案件管理 | 業務（Any-to-Any同期対象） | ④ 案件管理DB |
| 5 | サービス・商品 | 業務（Any-to-Any同期対象） | ⑤ サービス・商品DB |
| 6 | アクション管理 | 業務（Any-to-Any同期対象） | ⑥ アクション管理DB |
| 7 | 分析 | バッチ出力専用（読み取り用途） | 06節：段階別受注率・累積受注率 |
| 8 | クロスセル対象リスト | バッチ出力専用（読み取り用途） | 06節：クロスセル対象抽出 |
| 9 | 同期ログ | 却下データの退避先（05節） | コンフリクトの却下データ |

この9タブという数字は09節「全5タブ＋同期ログタブ」という記載と一致しない。
これは仕様書に明記の無い新規論点として扱い、コーディネーター側での確定を仰ぐこと
（10_保留・要確認事項 Q-01〜Q-10 に対する追加論点、便宜上ここではQ-11として言及する）。

> **Q-11（新規・要確認）**: スプレッドシートのタブ構成は09節の「全5タブ」ではなく、
> 6DB業務タブ全部＋分析タブ＋クロスセル対象タブ＋同期ログタブの計9タブとして
> 実装を進めた（本メモの決定理由を参照）。09節の「全5タブ」という数字自体を
> 訂正すべきか、あるいは業務タブを絞り込む意図が別途あったのかを確認したい。

## 各タブの列構成

### 業務タブ（1〜6）

各DBスキーマの `properties_synced_to(Tool.SPREADSHEET)`
（`sync_scope` が `ALL_TOOLS` または `SPREADSHEET_ONLY` のプロパティ）の並び順をそのまま
列見出しとし、末尾に `削除フラグ` 列を追加する。

- `NOTION_ONLY` / `INTERNAL` のプロパティ（例: 取引先マスターの「親取引先」、連絡先の
  「Eight連携ID」、アクション管理の「録画・録音URL」、各DB共通の `kintone_ID` /
  `created_at` 等）はスプレッドシートに同期されないため列を作らない。
- 「エリア属性データ」「エリアポテンシャルスコア」（取引先マスター、`sync_scope: スプシのみ`）
  はNotionに存在しないスプレッドシート固有列だが、`SpreadsheetSyncTarget` が通常の
  プロパティと同様に読み書きするため、他の列と区別せず同じ並びに含めている。
- `削除フラグ` は `src/sync_engine/sync_targets/spreadsheet_sync.py` の
  `_DELETE_FLAG_COLUMN`（論理削除の実装）に対応する、DBスキーマ上には存在しない
  スプレッドシート固有列。全業務タブ共通で末尾に追加する。
- 列の並び順とヘッダー名は `gas/tabDefinitions.js` の `BUSINESS_TABS` に定義している。
  GASはPython側の `src/db_schema/*.py` を実行時にimportできないため、この定義は
  手動でPython側と同期させる必要がある（プロパティ追加・変更時は両方を更新すること）。
- `autoColumns`（各タブ定義のフィールド）は、対応する `src/db_schema/*.py` で
  `RequirementLevel.AUTO`（`PropertyDefinition.is_auto`）と定義されている列のうち、
  スプレッドシートに同期される列（`headers` に含まれるもの）の一覧。`setupTemplate.js`
  の `setupTab_` がこれらの列に警告色の背景とセルコメントを付け、営業担当が
  システム自動算出項目を誤って手入力しないよう視覚的に注意喚起する。

### スキーマ変更時のチェックリスト（ドリフト検知）

`tabDefinitions.js` と `src/db_schema/*.py` は手動同期のため、Python側でDBスキーマの
プロパティ（特に `sync_scope`・`requirement`）を追加・変更した場合は、以下を確認すること。
自動検知の仕組みは無いため、レビュー時にこのチェックリストで手動突き合わせる。

1. 追加・変更したプロパティの `sync_scope` が `ALL_TOOLS` または `SPREADSHEET_ONLY` の場合、
   対応する `BUSINESS_TABS` の該当タブの `headers` に列名を追加・変更したか
   （`sync_scope` を `NOTION_ONLY`/`INTERNAL` に変更した場合は逆に `headers` から削除したか）。
2. プロパティの `requirement` が `RequirementLevel.AUTO` の場合、対応するタブの
   `autoColumns` にも列名を追加したか（`AUTO` から `REQUIRED`/`OPTIONAL` に変更した場合は
   `autoColumns` から削除したか）。
3. `python -c "from src.db_schema.registry import ALL_SCHEMAS; from src.db_schema.base import Tool; [print(s.spreadsheet_sheet_name, [p.name for p in s.properties_synced_to(Tool.SPREADSHEET)]) for s in ALL_SCHEMAS]"`
   の出力（スプレッドシートに同期されるべきプロパティ名一覧）と `gas/tabDefinitions.js` の
   `BUSINESS_TABS` の各タブの `headers`（末尾の `削除フラグ` を除く）が一致することを
   目視確認する。
4. `node --test gas/payloadUtils.test.js` を実行し、`tabDefinitions` 関連のテスト
   （重複列チェック・`autoColumns` が `headers` のサブセットであることのチェック等）が
   通ることを確認する。

### 分析タブ・クロスセル対象リストタブ（7・8）

06節の分析ロジック実装（`09_開発ロードマップ` T-08、Phase 3）はこのタスク（T-02、
Phase 1）のスコープ外のため、ここでは夜間/週次バッチが書き込む先として空のヘッダー行
だけを用意するテンプレートとした。列構成は `src/analytics/win_rate.py`
（`stage_win_rates` / `cumulative_win_rates`）・`src/analytics/win_pattern.py`
（`extract_cross_sell_targets`）の出力項目から素直に導出した最小限の構成であり、
T-08実装時に見直される前提とする。

これら2タブはGAS onEditの同期対象（`BUSINESS_SHEET_NAMES`）に含めていない。
バッチ処理による一方向の出力先であり、05節のAny-to-Any相互同期・却下データ退避の
対象ではないため。

### 同期ログタブ（9）

列構成は既存実装 `src/sync_engine/sync_targets/spreadsheet_sync.py` の
`append_conflict_log` が書き込む列（対象ID・項目名・採用値・却下値・却下元ツール・
発生日時）とそのまま一致させた（`gas/tabDefinitions.js` の `SYNC_LOG_TAB`）。
このタブもGAS onEditの同期対象には含めない（同期エンジンからの一方向の書き込み専用）。

## GAS実装との対応

- `gas/tabDefinitions.js`: 上記のタブ・ヘッダー定義（純粋なデータ、Node.jsからも参照可能）。
- `gas/setupTemplate.js`: `setupAll()` が全タブの作成・ヘッダー設定と、onEdit用
  インストーラブルトリガーの登録を行う（`scripts/setup_notion_databases.py` の
  スプレッドシート版）。
- `gas/onEdit.js`: 業務タブ（`BUSINESS_SHEET_NAMES`）の2行目以降が編集された場合のみ
  Webhookへ送信する。無限ループ防止の設計判断はファイル内コメントを参照。
- `gas/payloadUtils.js` / `gas/payloadUtils.test.js`: GAS固有APIに依存しない純粋関数と
  そのNode.jsテスト（`node --test gas/payloadUtils.test.js`）。検証方法の詳細は
  `gas/onEdit.js` 冒頭コメントを参照。
