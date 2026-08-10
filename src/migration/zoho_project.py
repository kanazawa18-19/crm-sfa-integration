"""Zoho 案件 → ④案件管理DB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、26,012件）:
- 多くのプロパティ名がPROJECT_SCHEMAとほぼ1対1で一致する（初期費用/月額費用/メモ/
  サイトコントローラー/かつやさん/ネックポイント/失注理由/アクション日/
  「契約日 / 予想契約日」等）。このZohoインスタンスの案件モジュールはNotion側の
  プロパティに直接対応するようカスタム構築された形跡がある。
- 「粗利」「個人粗利」はPROJECT_SCHEMA上FORMULA型、「予算組のタイミング」「決算月」は
  ROLLUP型で、いずれもNotion側で自動計算される読み取り専用プロパティのため、
  同名列がZoho側にあっても書き込み対象から除外する（書き込もうとすると
  build_notion_properties()で確実に失敗する）。
- 「サイトコントローラー」はPROJECT_SCHEMA上MULTI_SELECT型のため、Zoho側の単一自由記述
  値を1要素のリストへ変換する。「かつやさん」はCHECKBOX型のため、Zoho側の
  "true"/"false"文字列を実際のbool値へ変換する（Python の bool("false") は
  True になってしまうため、文字列比較で明示的に判定する必要がある）。
- 「営業ステータス」プロパティ自体は実データで100%空欄。実際のステータス相当情報は
  「ステージ」列（契約済/失注/解約（処理済み）/返信なし等19種類）に入っている。
  金沢さんの方針「Notionの営業ステータスをマスターにしたくない、Zohoの生の値を
  そのまま使いたい」（2026-08-10確認済み）により、圧縮・変換せずZohoの生の値を
  そのまま「営業ステータス」へ反映する。Notion側の選択肢にはkintone由来の既存11種に
  加えてこのZohoの19種も追加済み（PROJECT_SCHEMA/classify_status()参照）。
- 取引先へのリレーション解決は、Zoho内部ID（「取引先名.id」5.0%）と過去の連携作業で
  埋め込まれたNotionページ直リンク（「【Notion】取引先マスター」1.5%）を合わせても
  6.5%程度しか手がかりが無い。アクション（93.9%）と異なり、これは案件データ自体の
  制約であり、名寄せロジックで解決できる問題ではない。
- 「提案サービス」は自由記述の単一値（区切り文字での複数値は実データで未確認だが、
  安全のためparse_multi_value()で複数値にも対応させておく）。
"""

from __future__ import annotations

from src.migration._utils import extract_notion_page_id, parse_multi_value


def transform_zoho_project(record: dict[str, str]) -> dict[str, object]:
    """Zoho 案件 1レコードを ④案件管理DB のプロパティ値へ変換する。

    取引先マスター・サービス・商品へのリレーションはこの時点では解決せず、
    後続の解決ステップ用に `_` プレフィックス付きの手がかりのまま残す。
    """
    initial_fee = record.get("初期費用") or None
    monthly_fee = record.get("月額費用") or None
    client_zoho_id = record.get("取引先名.id") or None
    client_notion_page_id = extract_notion_page_id(record.get("【Notion】取引先マスター"))
    site_controller = (record.get("サイトコントローラー") or "").strip()

    return {
        "zoho_ID": record.get("データID", ""),
        "案件名": record.get("案件名", ""),
        "営業ステータス": record.get("ステージ") or None,
        "初期費用": float(initial_fee) if initial_fee is not None else None,
        "月額費用": float(monthly_fee) if monthly_fee is not None else None,
        "契約日 / 予想契約日": record.get("契約日 / 予想契約日") or None,
        "メモ": record.get("メモ") or None,
        "サイトコントローラー": [site_controller] if site_controller else [],
        "かつやさん": (record.get("かつやさん") or "").strip().lower() == "true",
        "ネックポイント": record.get("ネックポイント") or None,
        "失注理由": record.get("失注理由") or None,
        "アクション日": record.get("アクション日") or None,
        "メールアドレス": record.get("メールアドレス") or None,
        "電話番号": record.get("電話番号") or None,
        "_サービス名リスト": parse_multi_value(record.get("提案サービス")),
        "_取引先_zoho_id": client_zoho_id,
        "_取引先_notion_page_id": client_notion_page_id,
    }
