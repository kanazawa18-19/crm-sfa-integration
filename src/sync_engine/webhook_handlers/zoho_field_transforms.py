"""Zoho フィールドラベル → Notionプロパティ名 の変換テーブル（db_key別、部分更新用）。

zoho_webhook.py（Notification Webhook）は`affected_values`から得たZohoフィールド
ラベル（`src.sync_engine.zoho_field_mapping.resolve_zoho_field_label()`でapi_nameから
変換済み）を、そのままDatabaseSchemaのプロパティ名として扱っていた。しかし実際には
Zohoラベルがそのまま同名のNotionプロパティに対応するとは限らず（例:
Zohoの「ステージ」列 → Notionの「営業ステータス」プロパティ）、また値の形式変換
（日付の正規化・文字列boolのbool化・カンマ区切りの複数値分解等）が必要なプロパティも
ある（2026-08-12、実際のZoho本番編集がNotionへ反映されなかったことで発覚したBLOCKER）。

この変換ロジックは新規に考案したものではなく、一度限りの一括移行コード
`src/migration/zoho_project.py`の`transform_zoho_project()`で、金沢さんの承認を得て
フィールドごとに既に確定させたものと同一。`transform_zoho_project()`は移行CSVの
1レコード（全列揃った状態）を丸ごと変換する前提のため、Webhookの部分更新（delta、
1〜数フィールドのみ）にはそのまま使えない。そのため、同じフィールドごとの判断を
「Zohoラベル1個 → (Notionプロパティ名, 値変換関数)」という1フィールド単位で引ける形へ
移植したのが本モジュール。値変換の実装自体（`normalize_date`/`parse_multi_value`/
`_parse_bool`/`_parse_first_touch`）は重複させずzoho_project.py/_utils.pyから再利用する。

当初は「project」（Zoho案件モジュール = Notion④案件管理DB）のみを対象としていたが、
2026-08-12、残る5モジュール（chain/action/client_master/contact/product）についても
同じ方式で対応済みの`ZOHO_LABEL_FIELD_MAPPINGS`エントリを追加した。追加する場合は
`ZOHO_LABEL_FIELD_MAPPINGS`にdb_key単位でエントリを追加していけば拡張できる構造にしてある。

■ chain（CustomModule3）に関する注意（2026-08-12、モジュール取り違えを調査・修正済み）:
当初`CHAIN_SCHEMA.zoho_api_module`には誤って"CustomModule1"というプレースホルダ値が
割り当てられており、config/zoho_field_mapping.json上もCustomModule1セクションには
「チェーン名・グループ名」「アプローチ状況」「本社」等のラベルが1つも登場しない
（実際のCustomModule1のplural_labelは"商談1"で、案件寄りの別モジュール）ため、一時
「実質デッドコードの可能性」としてフラグを立てていた。その後、Zoho本番API
（`GET /crm/v3/settings/modules`）で実際のカスタムモジュール一覧を確認した結果、
チェーンの実際のモジュールは`CustomModule3`（plural_label="チェーン"）と判明したため、
`src/db_schema/chain.py`の`CHAIN_SCHEMA.zoho_api_module`を"CustomModule3"へ修正し、
config/zoho_field_mapping.jsonにも実際のCustomModule3セクション（ライブAPIから取得、
41フィールド）を反映済み。以下の`_CHAIN_ZOHO_LABEL_TO_NOTION_FIELD`が使うZohoラベルは
`transform_zoho_chain()`（一括移行時のCSV由来）の判断をそのまま移植したものだが、
CustomModule3の実際のラベルと1つずつ突き合わせた結果すべて一致することを確認済みのため、
デッドコードではなく実際のWebhook通知でも正しく解決される。

■ ⑥アクション履歴の取引先マスターリレーション（2026-08-25、Round2追加）: 「案件名」「取引先」
「【Notion】取引先マスター」は当初いずれも「1フィールド単位のWebhook部分更新には不適切」
として意図的に対象外だったが、「取引先」（field6）・「【Notion】取引先マスター」（field22）
の2つは`src.relation_sync.resolve_zoho.resolve_zoho_action_client_master_relation()`
（field22の埋め込みNotionページIDヒント優先、無ければfield6の生の会社名を
`resolve_client_master_relation()`で名寄せ）による例外対応を行った（kintone側の`client_name`
と同じ設計思想、2026-08-25）。「案件名」は引き続き対象外（案件(project)リレーションは
kintone側と同じ理由でスコープ外。下記`_ACTION_ZOHO_LABEL_TO_NOTION_FIELD`直前のコメント参照）。

なぜ「ステージ」の値を圧縮・変換せず「営業ステータス」へそのまま書き込むのか:
`transform_zoho_project()`のモジュールdocstring（2026-08-10確認）が既に記録している通り、
Notion「営業ステータス」プロパティ自体は実データで100%空欄であり、実質的なステータス情報は
Zoho「ステージ」列（契約済/失注/解約（処理済み）/返信なし等）にしか無い。金沢さんの方針
「Notionの営業ステータスをマスターにしたくない、Zohoの生の値をそのまま使いたい」により、
意図的に変換・圧縮せずZohoの生の値をそのまま書き込む。将来「もっと賢く変換すべきでは」と
直したくなるかもしれないが、これは既に確認済みの製品判断であり、バグではない。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from src.migration._utils import normalize_date, parse_multi_value
from src.migration.kintone_client_master import normalize_customer_type
from src.migration.zoho_action import classify_zoho_action_type
from src.migration.zoho_chain import normalize_approach_status
from src.migration.zoho_client_master import normalize_prefecture
from src.migration.zoho_project import _parse_bool, _parse_first_touch
from src.relation_sync.resolve_zoho import (
    ZohoActionRecordClient,
    resolve_zoho_action_client_master_relation,
)
from src.sync_engine.webhook_handlers._relation_guard import CLIENT_MASTER_RELATION_PROPERTY

# 変換関数の戻り値は`None`が「Notion側の値を明示的にクリアする」という意味で使われている
# 既存の全エントリと共通（例: `lambda v: v or None`）。しかし
# `_resolve_client_master_for_zoho_action`だけは意味が異なり、「まだ解決できていない」ことを
# 表す（この場合は取引先マスターのリレーション自体を触らずスキップしたい。誤って
# `{"relation": []}`を送ってしまうと既存のリレーションを消してしまう）。既存のNoneの意味と
# 衝突するため、このモジュール専用のセンチネル値`SKIP_FIELD`を返し、呼び出し元
# （zoho_webhook.py）側で区別する（kintone_field_transforms.pyの同名センチネルと同じ設計）。
SKIP_FIELD = object()

# `resolve_zoho_action_client_master_relation()`はRelationReviewQueueへの記録・Zoho APIでの
# レコード全体取得（field22/field6のどちらか一方しか今回のWebhook通知に含まれない場合の補完）に
# 呼び出し元のZohoレコードID・当該レコードの変更差分(delta)・Zohoクライアントを必要とするが、
# このモジュールの変換関数は既存の全エントリと同じ「1フィールドの値→Notion書き込み用の値」
# という単純な1引数シグネチャ（`Callable[[Any], Any]`）に統一されている。
# `kintone_field_transforms.py`の`kintone_action_record_context()`と同じ`contextvars`の手法を
# 再利用し、zoho_webhook.py側がレコードごとのフィールド処理ループを開始する前に一度だけ設定する
# （`zoho_action_relation_context()`参照）。


@dataclass(frozen=True)
class _ZohoActionRelationContext:
    record_id: str
    changed_values: Mapping[str, Any]
    zoho_client: ZohoActionRecordClient | None


_current_zoho_action_relation_context: contextvars.ContextVar[
    _ZohoActionRelationContext | None
] = contextvars.ContextVar("current_zoho_action_relation_context", default=None)


@contextmanager
def zoho_action_relation_context(
    record_id: str,
    changed_values: Mapping[str, Any],
    zoho_client: ZohoActionRecordClient | None,
) -> Iterator[None]:
    """このwithブロック内での`_resolve_client_master_for_zoho_action()`呼び出しに、現在処理中
    のZohoレコードID・当該レコードのWebhook変更差分(delta)・Zohoクライアント（レコード全体
    取得用、省略可）を伝播させる。"""
    token = _current_zoho_action_relation_context.set(
        _ZohoActionRelationContext(
            record_id=record_id, changed_values=changed_values, zoho_client=zoho_client
        )
    )
    try:
        yield
    finally:
        _current_zoho_action_relation_context.reset(token)


def _resolve_client_master_for_zoho_action(_value: Any) -> Any:
    """⑥アクション履歴の「取引先」（field6）/「【Notion】取引先マスター」（field22）いずれかの
    変更を、「👨‍👩‍👧‍👦 取引先マスター」リレーションへ解決する。`_value`（変更された当該フィールド
    単体の値）は使わず、`zoho_action_relation_context()`で伝播された当該レコードの変更差分全体
    （field22/field6両方を必要とする優先順位判定のため）を参照する。解決できた場合はNotion
    page ID、できなかった場合（曖昧・候補なし・コンテキスト未設定）は`SKIP_FIELD`を返す。
    """
    ctx = _current_zoho_action_relation_context.get()
    if ctx is None:
        return SKIP_FIELD
    resolved = resolve_zoho_action_client_master_relation(
        record_id=ctx.record_id, changed_values=ctx.changed_values, zoho_client=ctx.zoho_client
    )
    return resolved if resolved is not None else SKIP_FIELD

# Zohoラベル -> (Notionプロパティ名, 値変換関数)
# 対象は transform_zoho_project() が実際にNotionプロパティへ書き込んでいるフィールドのみ。
# 以下は意図的に含めない（transform_zoho_project()のdocstring参照）:
# - `_`プレフィックスの内部専用キー（_サービス名リスト/_取引先_zoho_id/_取引先_notion_page_id）
#   ： 取引先マスター等へのリレーション解決が必要で、1フィールド単位のWebhook部分更新には
#   不適切な、別の複雑な処理のため対象外。
# - `zoho_ID`: Notionのプロパティ書き込み対象ではない。
# - `確度`: NotionのA/B/C/D選択肢とZoho側の0〜100パーセント値で尺度が異なり、機械的な
#   変換対応表が無い。
# - `例外スイッチ`/`ショット`: 対応するZoho列が実データと意味が一致しない。
# - FORMULA/ROLLUP型のNotionプロパティ（粗利/個人粗利/契約スピード/失注経過日数（日）/
#   初期フィー/フィー率/経過日数/予算組のタイミング/アクション日/決算月/チェーン本社/
#   アクションログ）: Notion側で自動計算される読み取り専用プロパティで、書き込もうとすると
#   確実に失敗する。
_PROJECT_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    # Zohoラベル == Notionプロパティ名だが、値変換が必要なもの（"同名だから変換不要"ではない）。
    "案件名": ("案件名", lambda v: v),
    "初期費用": ("初期費用", lambda v: float(v) if v not in (None, "") else None),
    "月額費用": ("月額費用", lambda v: float(v) if v not in (None, "") else None),
    "メモ": ("メモ", lambda v: v or None),
    "サイトコントローラー": ("サイトコントローラー", parse_multi_value),
    "かつやさん": ("かつやさん", _parse_bool),
    "問合せ": ("問合せ", _parse_bool),
    "ネックポイント": ("ネックポイント", lambda v: v or None),
    "失注理由": ("失注理由", lambda v: v or None),
    "失注日": ("失注日", normalize_date),
    "メールアドレス": ("メールアドレス", lambda v: v or None),
    "電話番号": ("電話番号", lambda v: v or None),
    "契約日 / 予想契約日": ("契約日 / 予想契約日", normalize_date),
    # Zoho標準フィールド「完了予定日」(Closing_Date)は、一括移行時に使ったカスタムフィールド
    # 「契約日 / 予想契約日」(field50)とは別物だが、2026-08-12に金沢さんの確認を得て、どちらの
    # 変更もNotionの同じ「契約日 / 予想契約日」プロパティへ反映する方針とした
    # （後から更新された方が同期される。特別な優先順位付けはしない）。
    "完了予定日": ("契約日 / 予想契約日", normalize_date),
    # Zohoラベル != Notionプロパティ名。
    # 「ステージ」→「営業ステータス」: Zohoの生の値をそのまま書き込む（変換しない）。
    # 上記モジュールdocstring参照。値変換関数を挟むと将来"賢い変換"を誤って追加しかねないため
    # 明示的に恒等関数にしている。
    "ステージ": ("営業ステータス", lambda v: v),
    "【Notion】テキスト": ("テキスト", lambda v: v or None),
    "【Notion】ファーストタッチ": ("ファーストタッチ", _parse_first_touch),
    "【Notion】担当者名": ("担当者名", lambda v: v or None),
    "決裁者": ("決裁者名", lambda v: v or None),
    "【Notion】次回アクション": ("次回アクション", lambda v: v or None),
    "【Notion】サービス数（施設数）": (
        "サービス数（施設数）",
        lambda v: float(v) if v not in (None, "") else None,
    ),
}

# 対象は transform_zoho_chain() が実際にNotionプロパティへ書き込んでいるフィールドのみ
# （src/migration/zoho_chain.py参照）。"zoho_ID"は内部専用キーのため対象外。
# 上記モジュールdocstring参照: CustomModule3（正しいチェーンモジュール）の実際のライブAPI
# ラベルとここで使うZohoラベルは一致することを確認済み。
_CHAIN_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "チェーン名・グループ名": ("グループ名", lambda v: v),
    "アプローチ状況": ("アプローチ状況", normalize_approach_status),
    "施設数": ("施設数", lambda v: v or None),
    "本社": ("本社", lambda v: v or None),
    "本社所在地": ("本社所在地", lambda v: v or None),
    "運営会社": ("運営会社", lambda v: v or None),
    "電話": ("電話", lambda v: v or None),
    "チェーンURL": ("URL", lambda v: v or None),
    "メモ": ("メモ", lambda v: v or None),
    "決裁": ("決裁", lambda v: v or None),
    "未導入店へのアプローチ": ("未導入店舗へのアプローチ", lambda v: v or None),
    "自動チェックイン機（URL）": ("自動チェックインURL", lambda v: v or None),
    "自動チェックイン機": ("自動チェックイン", lambda v: v or None),
    "最終更新日（最終アプローチ日）": ("最終アプローチ日", normalize_date),
}

# 対象は transform_zoho_action() が実際にNotionプロパティへ書き込んでいるフィールドのみ
# （src/migration/zoho_action.py参照）。以下は意図的に含めない:
# - "zoho_Act_ID": 内部専用キー。
# - "案件名"（`_案件_notion_page_id`用の手がかり）: 案件(project)リレーションは、kintone側
#   （`kintone_field_transforms.py`のモジュールdocstring参照）と同じ理由でスコープ外。
#   CustomModule2には「【Notion】案件」のような埋め込みNotionページIDヒントを持つフィールドが
#   存在せず、「案件名」は案件そのものを一意に絞り込める情報を持たない自由記述テキストのため、
#   自動選択はもちろんレビューキューへ積んでも人間が判断できる材料が無い（事前調査で確認済み、
#   2026-08-25）。
# - "取引先"/"【Notion】取引先マスター": 当初は`_取引先_zoho_id`/`_取引先_notion_page_id`用の
#   手がかりとして「1フィールド単位のWebhook部分更新には不適切」で対象外だったが、
#   2026-08-25（Round2）に`_resolve_client_master_for_zoho_action`による例外対応を行った
#   （下記エントリ参照、モジュールdocstring「⑥アクション履歴の取引先マスターリレーション」欄）。
# - "手当情報アップロード": FILES型で別モジュール（添付ファイル同期）が扱う対象。
# - "アクション種別": ACTION_SCHEMA上はSELECT型で書き込み可能だが、transform_zoho_action()では
#   Zoho側の同名列を直接読まず、「アクション名」の自由記述をclassify_zoho_action_type()で
#   キーワード分類した結果から間接的に算出している。1ラベル→1プロパティ固定の本テーブルの
#   構造では「アクション名」を同時に2プロパティ（title/select）へ書き込めないため、
#   より確実なtitleへの反映を優先しアクション種別は対象外とした（2026-08-12時点のライブ
#   フィールドマッピングには実は"アクション種別"という別列も存在するが、これはZoho側で
#   自由記述と独立して更新されうる値であり、classify_zoho_action_type()の分類結果とは
#   別物のため、その値をそのまま書き込むのは検証されていない判断になってしまう）。
# - "導入フローとスケジュール": ACTION_SCHEMA上書き込み可能なTEXT型プロパティで、ライブAPIにも
#   対応する列（"【Notion】導入フローとスケジュール"）が存在するが、transform_zoho_action()は
#   これを一度も書き込んでいない（移行時に対象外とされた理由の記載なし）ため、既存の
#   人手確認済み判断を踏襲する原則により、ここでも対象外のままにする。
# - ROLLUP/CREATED_TIME/CREATED_BY型のプロパティ（決済者/担当営業/案件 担当者名/提案サービス/
#   営業ステータス/作成日時/作成者）: 読み取り専用のため対象外。
# - "連絡先"/"👯‍♀️ チェーンリスト": リレーションだがtransform_zoho_action()に対応する
#   書き込みが無い。
def _zoho_action_type(value: Any) -> str | None:
    """Zohoの「アクション種別」(picklist)を、アクション履歴DBの選択肢へ正規化する。

    **選択肢が両者で一致していない。**（2026-08-31、実データで確認）

        Zoho   : テレアポ / メルアポ / 訪問商談 / WEB商談 / 電話商談
        Notion : テレアポ / 訪問商談 / オンライン商談 / メール / 問い合わせメール /
                 飛び込み / 自動メール / その他

    そのまま渡すとNotionのselectに無い値になるため、移行時に実データ27,238件で
    確認済みの`classify_zoho_action_type()`へ通す（メルアポ→メール、WEB商談→
    オンライン商談、電話商談→テレアポ。いずれも移行時に確認済みの寄せ方）。

    **未入力（空・`-None-`）は`None`を返して「その他」に丸めない。**
    アクション種別は必須プロパティで、丸めてしまうと入力漏れに気づけなくなる。
    Noneのままにしておけば、新規作成は`missing_required_properties`として
    Slackへ通知され、Zoho側の入力を促せる（それが通知文の意図）。
    """
    text = str(value or "").strip()
    if not text or text == "-None-":
        return None
    return classify_zoho_action_type(text)


_ACTION_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "アクション名": ("商談回数・電話回数・メール回数（何回目）", lambda v: v or ""),
    # 2026-08-31追加。**これが無かったため、Zoho発の新規アクションが1件も作られていなかった**
    # （必須プロパティ「アクション種別」が常に欠けて missing_required_properties で中止）。
    # Zoho側のデータもマッピングファイル（field7→アクション種別）も正しく、
    # このラベル→Notionプロパティの対応表だけが抜けていた。
    "アクション種別": ("アクション種別", _zoho_action_type),
    "アクション日": ("アクション日", normalize_date),
    "履歴メモ": ("履歴メモ", lambda v: v or None),
    "先方担当者": ("先方担当者", lambda v: v or None),
    # 「Notta」列を優先し、無ければ「録画・音声ファイル」を使うというtransform_zoho_action()の
    # 優先順位付けは、1レコード全体を一括変換する移行専用の挙動。Webhookの部分更新は1フィールド
    # ずつ独立して届くため優先順位の概念が無く、どちらのラベルも同じ「議事録・録画リンク」へ
    # 素直に反映する（最後に届いた方が勝つ、通常のdelta更新と同じ挙動）。
    "Notta": ("議事録・録画リンク", lambda v: v or None),
    "録画・音声ファイル": ("議事録・録画リンク", lambda v: v or None),
    # 取引先マスターリレーション（2026-08-25、Round2追加）。「取引先」（field6）・
    # 「【Notion】取引先マスター」（field22）のどちらが変更されても、同じ解決関数
    # （`_resolve_client_master_for_zoho_action`、field22の埋め込みヒント優先）を呼ぶ
    # （kintone側の`client_name`と同じ設計思想。上記モジュールdocstring参照）。
    "取引先": (CLIENT_MASTER_RELATION_PROPERTY, _resolve_client_master_for_zoho_action),
    "【Notion】取引先マスター": (
        CLIENT_MASTER_RELATION_PROPERTY,
        _resolve_client_master_for_zoho_action,
    ),
}

# 対象は transform_zoho_client_master() が実際にNotionプロパティへ書き込んでいるフィールドのみ
# （src/migration/zoho_client_master.py参照）。"zoho_ID"は内部専用キーのため対象外。
_CLIENT_MASTER_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "取引先名": ("取引先名", lambda v: v),
    "顧客種別": ("顧客種別", normalize_customer_type),
    "郵便番号": ("郵便番号", lambda v: v or None),
    "都道府県": ("都道府県", normalize_prefecture),
    "住所": ("住所", lambda v: v or None),
    # Zohoラベル != Notionプロパティ名。
    "電話番号": ("TEL", lambda v: v or None),
    "Fax": ("FAX", lambda v: v or None),
}

# 対象は transform_zoho_contact() が実際にNotionプロパティへ書き込んでいるフィールドのみ
# （src/migration/zoho_contact.py参照）。以下は意図的に含めない:
# - "zoho_ID": 内部専用キー。
# - "【Eight】会社名": 取引先マスターへの名寄せ解決用の`_会社名`キーであり、1フィールド単位の
#   Webhook部分更新には不適切（projectの`_`プレフィックスキーと同じ扱い）。
# - "名刺交換日"/"【Eight】名刺交換者"/"名刺交換者"/"Eight連携ID"/"人事異動フラグ":
#   CONTACT_SCHEMA上いずれも`RequirementLevel.AUTO`かつ「Eight連携で自動投入」専用の
#   プロパティ（`SyncScope.NOTION_ONLY`）。金沢さん確認済みの方針により今回のZoho連携では
#   意図的に書き込まない（transform_zoho_contact()のdocstring参照）。
_CONTACT_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    # Zohoラベル != Notionプロパティ名。
    "氏名": ("名前", lambda v: v),
    "部署名": ("部署", lambda v: v or None),
    "役職": ("役職", lambda v: v or None),
    "メール": ("メールアドレス", lambda v: v or None),
    "携帯電話": ("携帯番号", lambda v: v or None),
    "TEL会社": ("直通TEL", lambda v: v or None),
}

# 対象は transform_zoho_product() が実際にNotionプロパティへ書き込んでいるフィールドのみ
# （src/migration/zoho_product.py参照）。以下は意図的に含めない:
# - "zoho_ID": 内部専用キー。
# - "課金形態"（PRODUCT_SCHEMA上REQUIRED、SELECT型）: transform_zoho_product()では
#   Zoho側に対応する列が存在しないため`_DEFAULT_BILLING_TYPE`（"イニシャルスポット"）を
#   常に既定値として書き込んでいるだけで、どのZohoラベルからも導出されていない。
#   1フィールド単位のWebhookマッピングとして流用できる「Zohoラベル→値」の対応が
#   存在しないため対象外（実データ精査後に手動調整する前提という元の移行方針を踏襲）。
_PRODUCT_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    # Zohoラベル != Notionプロパティ名。
    "サービス・商品名": ("名前", lambda v: v),
    "初期費用": ("標準初期費用", lambda v: float(v) if v not in (None, "") else None),
    "月額費用": ("標準月額費用", lambda v: float(v) if v not in (None, "") else None),
}

# db_key -> (Zohoラベル -> (Notionプロパティ名, 値変換関数))。
ZOHO_LABEL_FIELD_MAPPINGS: dict[str, dict[str, tuple[str, Callable[[Any], Any]]]] = {
    "project": _PROJECT_ZOHO_LABEL_TO_NOTION_FIELD,
    "chain": _CHAIN_ZOHO_LABEL_TO_NOTION_FIELD,
    "action": _ACTION_ZOHO_LABEL_TO_NOTION_FIELD,
    "client_master": _CLIENT_MASTER_ZOHO_LABEL_TO_NOTION_FIELD,
    "contact": _CONTACT_ZOHO_LABEL_TO_NOTION_FIELD,
    "product": _PRODUCT_ZOHO_LABEL_TO_NOTION_FIELD,
}
