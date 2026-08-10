"""Zoho アクション → ⑥アクション履歴DB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、27,238件）:
- ACTION_SCHEMAのtitleプロパティ（「商談回数・電話回数・メール回数（何回目）」）は
  自由記述の実質のアクション内容記述である設計（既存db_schemaのdocstring参照）。
  Zohoの「アクション名」列（例:「テレアポ」「【電話】4回目」「テレアポ↓（大野）」）が
  まさにこの自由記述に相当するため、そのままtitleへ反映する（kintone移行時のように
  連番IDへ差し替えない。金沢さん確認済み: 元の登録名をそのまま残したい）。
- 「取引先.id」（Zohoの取引先への直接参照）と「【Notion】取引先マスター」（過去の連携作業
  で埋め込まれたNotionページの直リンク）は完全に排他的（重複ゼロ）。両方を合わせると
  89.2%のアクションで取引先との関連付けができる。
- 「案件名」列は名称だがリレーションIDではなく、Notionページへの直リンクが埋め込まれた
  自由記述（10.0%）。案件へのリレーション解決の主な手がかりとなる
  （Zoho内部の「商談.id」は実データで常に空欄、中間テーブル「案件×アクション」も887件
  しかなく主要な手段にはならない）。
- 「先方担当者」は自由記述テキスト（kintoneのアクション管理と同様、正式なリレーションは
  無い）。ACTION_SCHEMAの「先方担当者」もTEXT型のため、そのまま文字列として反映する。
- 「Notta」（0.7%）「録画・音声ファイル」（0.2%）はいずれもNotta.ai（議事録・文字起こし
  サービス）のURLで、Notion側に対応プロパティが無かったため2026-08-10に金沢さんの
  指摘で「議事録・録画リンク」（URL型）を新規作成した。「Notta」列を優先し、無ければ
  「録画・音声ファイル」を使う。
"""

from __future__ import annotations

from src.migration._utils import extract_notion_page_id

# アクション名の部分一致パターンから既存の「アクション種別」選択肢へ分類する対応表。
# 実データ27,238件全件を分類した結果（2026-08-10、金沢さん確認済み）:
#   オンライン商談系（「WEB商談」「web商談」等、大小文字を無視して"web"を含む）→ オンライン商談
#   訪問商談系（「訪問」を含む。例:「訪問商談」「商談（訪問）」）→ 訪問商談
#   テレアポ系（「テレアポ」「【電話】」「電話」を含む。「【テレアポ】1回目」等の
#     プレフィックスでない出現にも対応するため部分一致で判定）79.0% → テレアポ
#   メール系（「メルアポ」「メール」を含む）9.7% → メール（メルアポ＝メールでのアポ取得）
#   飛び込み系 → 飛び込み
#   問い合わせ系 → 問い合わせメール
#   上記いずれにも該当しない「商談」系（「【商談】1回目」等、訪問かオンラインかテキストから
#     区別できないもの）・その他（LINE等）→ その他
#     （その他へ寄せても、実際の登録名自体はtitleへそのまま反映されるので情報は失われない、
#     という金沢さんの確認済み方針）
_ONLINE_KEYWORDS = ("web",)  # 小文字化して判定（WEB/web/Web表記ゆれ対応）
_VISIT_KEYWORDS = ("訪問",)
_TELEAPO_KEYWORDS = ("テレアポ", "【電話】", "電話")
_MAIL_KEYWORDS = ("メルアポ", "メール")
_COLD_CALL_PREFIXES = ("飛び込み", "飛込")
_INQUIRY_KEYWORDS = ("問い合わせ", "問合せ", "問合")


def classify_zoho_action_type(action_name: str | None) -> str:
    """Zohoの自由記述「アクション名」から⑥アクション履歴DBの「アクション種別」を推定する。"""
    name = (action_name or "").strip()
    name_lower = name.lower()
    if any(keyword in name_lower for keyword in _ONLINE_KEYWORDS):
        return "オンライン商談"
    if any(keyword in name for keyword in _VISIT_KEYWORDS):
        return "訪問商談"
    if any(keyword in name for keyword in _TELEAPO_KEYWORDS):
        return "テレアポ"
    if any(keyword in name for keyword in _MAIL_KEYWORDS):
        return "メール"
    if name.startswith(_COLD_CALL_PREFIXES):
        return "飛び込み"
    if any(keyword in name for keyword in _INQUIRY_KEYWORDS):
        return "問い合わせメール"
    return "その他"


def transform_zoho_action(record: dict[str, str]) -> dict[str, object]:
    """Zoho アクション 1レコードを ⑥アクション履歴DB のプロパティ値へ変換する。

    取引先マスター・案件管理へのリレーションはこの時点では解決せず、後続の解決ステップ用に
    `_` プレフィックス付きの手がかり（Zoho内部ID／埋め込みNotionページID）のまま残す。
    """
    action_name = record.get("アクション名", "")
    client_zoho_id = record.get("取引先.id") or None
    client_notion_page_id = extract_notion_page_id(record.get("【Notion】取引先マスター"))
    project_field = record.get("案件名") or ""
    project_notion_page_id = extract_notion_page_id(project_field)

    return {
        "zoho_Act_ID": record.get("データID", ""),
        "商談回数・電話回数・メール回数（何回目）": action_name,
        "アクション種別": classify_zoho_action_type(action_name),
        "アクション日": record.get("アクション日") or None,
        "履歴メモ": record.get("履歴メモ") or None,
        "先方担当者": record.get("先方担当者") or None,
        "議事録・録画リンク": record.get("Notta") or record.get("録画・音声ファイル") or None,
        "_取引先_zoho_id": client_zoho_id,
        "_取引先_notion_page_id": client_notion_page_id,
        "_案件_notion_page_id": project_notion_page_id,
    }
