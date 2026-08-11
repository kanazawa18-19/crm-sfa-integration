"""Zoho 案件 → ④案件管理DB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、26,012件）:
- 多くのプロパティ名がPROJECT_SCHEMAとほぼ1対1で一致する（初期費用/月額費用/メモ/
  サイトコントローラー/かつやさん/ネックポイント/失注理由/「契約日 / 予想契約日」/
  担当者名/決裁者名/次回アクション/テキスト/サービス数（施設数）/失注日/
  ファーストタッチ/問合せ 等）。このZohoインスタンスの案件モジュールはNotion側の
  プロパティに直接対応するようカスタム構築された形跡がある。
- 「粗利」「個人粗利」「契約スピード」「失注経過日数（日）」「初期フィー」「フィー率」
  「経過日数」はPROJECT_SCHEMA上FORMULA型、「予算組のタイミング」「アクション日」
  「決算月」「チェーン本社」「アクションログ」はROLLUP型で、いずれもNotion側で自動計算
  される読み取り専用プロパティのため、同名列がZoho側にあっても書き込み対象から除外する
  （書き込もうとするとbuild_notion_properties()で確実に失敗する。"アクション日"は
  当初誤って書き込み対象に含めてしまっていたが、ROLLUP型と判明したため削除した）。
- 「確度」はPROJECT_SCHEMA上A/B/C/Dの4段階選択肢だが、Zoho側の「確度」列は0〜100の
  パーセント値で意味・尺度が異なる（機械的な変換対応表が無い）上、実データの97.3%が
  単に「0」（未設定のデフォルト値）で実質意味を持たないため、マッピングしない。
- 「例外スイッチ」「ショット」は対応するZoho列が実データで常にfalseかつ意味も一致しない
  （「ショット」はNotion側はNUMBER型だがZoho側は常にfalseのフラグ列で無関係）ため、
  マッピングしない。
- 「サイトコントローラー」はPROJECT_SCHEMA上MULTI_SELECT型。当初「Zoho側は単一自由記述」
  という前提で1要素リストへ包むだけの実装にしていたが、本番投入時に実データで
  "なし, リンカーン"のようなカンマ区切りの複数値（829件中6件）が見つかり、Notion API側
  （multi_selectのoption名にカンマを含められない）から`HTTP 400: Invalid multi_select
  option, commas not allowed`で拒否される事故が発生した（2026-08-11）。「ファーストタッチ」
  と同じくカンマ区切りの複数値がありうる列だったため、`parse_multi_value()`で分割する
  よう修正した（分割後の値は全てPROJECT_SCHEMAの登録済み選択肢と一致することを実データで
  確認済み）。「かつやさん」「問合せ」は
  CHECKBOX型のため、Zoho側の"true"/"false"文字列を実際のbool値へ変換する
  （Python の bool("false") は True になってしまうため、文字列比較で明示的に判定する
  必要がある）。
- 「営業ステータス」プロパティ自体は実データで100%空欄。実際のステータス相当情報は
  「ステージ」列（契約済/失注/解約（処理済み）/返信なし等）に入っている。
  金沢さんの方針「Notionの営業ステータスをマスターにしたくない、Zohoの生の値を
  そのまま使いたい」（2026-08-10確認済み）により、圧縮・変換せずZohoの生の値を
  そのまま「営業ステータス」へ反映する。Notion側の選択肢にはkintone由来の既存11種に
  加えてこのZohoの21種も追加済み（PROJECT_SCHEMA/classify_status()参照）。
- 取引先へのリレーション解決は、Zoho内部ID（「取引先名.id」5.0%）と過去の連携作業で
  埋め込まれたNotionページ直リンク（「【Notion】取引先マスター」1.5%）を合わせても
  6.8%程度しか手がかりが無い。アクション（93.9%）と異なり、これは案件データ自体の
  制約であり、名寄せロジックで解決できる問題ではない。
- 「提案サービス」は自由記述の単一値（区切り文字での複数値は実データで未確認だが、
  安全のためparse_multi_value()で複数値にも対応させておく）。
- 添付ファイル（見積書・申込書契約書受注書・個別提案資料）は別モジュール
  （src/migration/zoho_attachments.py）で扱う。
"""

from __future__ import annotations

from src.db_schema.project import PROJECT_SCHEMA
from src.migration._utils import extract_notion_page_id, normalize_date, parse_multi_value


def _parse_first_touch(raw: str | None) -> list[str]:
    """「ファーストタッチ」はカンマ区切りの複数値。既存選択肢に無い値は無言で捨てず
    ログに残しつつ除外する（normalize_customer_type等と同じ方針）。"""
    if not raw or not raw.strip():
        return []
    valid_options = set(PROJECT_SCHEMA.get_property("ファーストタッチ").options)
    values = [v.strip() for v in raw.split(",") if v.strip()]
    return [v for v in values if v in valid_options]


def _parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() == "true"


def transform_zoho_project(record: dict[str, str]) -> dict[str, object]:
    """Zoho 案件 1レコードを ④案件管理DB のプロパティ値へ変換する。

    取引先マスター・サービス・商品へのリレーションはこの時点では解決せず、
    後続の解決ステップ用に `_` プレフィックス付きの手がかりのまま残す。
    """
    initial_fee = record.get("初期費用") or None
    monthly_fee = record.get("月額費用") or None
    service_count = record.get("【Notion】サービス数（施設数）") or None
    client_zoho_id = record.get("取引先名.id") or None
    client_notion_page_id = extract_notion_page_id(record.get("【Notion】取引先マスター"))

    return {
        "zoho_ID": record.get("データID", ""),
        "案件名": record.get("案件名", ""),
        "営業ステータス": record.get("ステージ") or None,
        "初期費用": float(initial_fee) if initial_fee is not None else None,
        "月額費用": float(monthly_fee) if monthly_fee is not None else None,
        "契約日 / 予想契約日": normalize_date(record.get("契約日 / 予想契約日")),
        "メモ": record.get("メモ") or None,
        "テキスト": record.get("【Notion】テキスト") or None,
        "サイトコントローラー": parse_multi_value(record.get("サイトコントローラー")),
        "ファーストタッチ": _parse_first_touch(record.get("【Notion】ファーストタッチ")),
        "かつやさん": _parse_bool(record.get("かつやさん")),
        "問合せ": _parse_bool(record.get("問合せ")),
        "ネックポイント": record.get("ネックポイント") or None,
        "失注理由": record.get("失注理由") or None,
        "失注日": normalize_date(record.get("失注日")),
        "担当者名": record.get("【Notion】担当者名") or None,
        "決裁者名": record.get("決裁者") or None,
        "次回アクション": record.get("【Notion】次回アクション") or None,
        "サービス数（施設数）": float(service_count) if service_count is not None else None,
        "メールアドレス": record.get("メールアドレス") or None,
        "電話番号": record.get("電話番号") or None,
        "_サービス名リスト": parse_multi_value(record.get("提案サービス")),
        "_取引先_zoho_id": client_zoho_id,
        "_取引先_notion_page_id": client_notion_page_id,
    }
