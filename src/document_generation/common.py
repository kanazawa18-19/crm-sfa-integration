"""ドキュメント生成3種（見積書・申込書・契約書）で共有する型・例外・テンプレート解決処理。"""

from __future__ import annotations

from dataclasses import dataclass

from src.document_generation.service_mapping import resolve_template_service
from src.document_generation.template_registry import TemplateInfo, TemplateRegistry


class TemplateNotFoundError(Exception):
    """テンプレートが解決できなかった場合（マッピング対象外のサービス、DB未登録）に送出する。"""


# 見積書・申込書テンプレートのスプレッドシート内で、差し込み対象として複製する空タブの名前。
# 実データ確認の結果、テンプレートファイルには空タブが存在せず全タブが実在クライアントの
# 過去案件だったため、「先頭タブ＝空の雛形」という以前の前提を廃止し、この名前と完全一致する
# タブのみを対象にする（テンプレート管理者側で、この名前の空タブを用意してもらう運用）。
TEMPLATE_SHEET_TITLE = "雛形"


class TemplateSheetNotFoundError(Exception):
    """テンプレートのスプレッドシート内に`TEMPLATE_SHEET_TITLE`と完全一致するタブが
    見つからなかった場合に送出する。"""


class ContractGenerationError(Exception):
    """契約書生成中に、安全に自動生成を続けられない状態を検知した場合に送出する。

    例: 宛先プレースホルダ「〇〇」の置換件数が想定外（0件または2件以上）だった場合。
    法的文書のため、日付・金額欄等の意図しない箇所まで書き換わった契約書をそのまま
    利用者に渡してしまう事故を避け、生成自体を失敗させて手動確認を促す。
    """


# 3生成器共通のbaseline note。ラベル部分一致検索・先頭タブ前提等、既知の制約があるため、
# 生成結果を無条件に信用せず必ず内容を確認してから使ってもらうための注意喚起
# （obasan-qualityレビュー: 部分一致の潜在リスクが利用者に伝わらないとの指摘を反映）。
BASELINE_NOTE = (
    "自動生成された書類です。内容（宛先・件名・金額・印影等）を必ず確認してから送付してください。"
)


@dataclass(frozen=True)
class DocumentResult:
    content: bytes
    file_name: str
    mime_type: str
    notes: list[str]


def resolve_template(
    category: str, proposed_services: list[str], registry: TemplateRegistry
) -> TemplateInfo:
    """案件の「提案サービス」一覧から、テンプレート管理DBのテンプレートを解決する。

    複数の提案サービスがある場合、テンプレート側へマッピングでき、かつテンプレートが実在する
    最初のサービスを採用する。1件も解決できない場合はTemplateNotFoundErrorを送出する。
    """
    tried: list[str] = []
    for project_service in proposed_services:
        template_service = resolve_template_service(project_service)
        if template_service is None:
            continue
        tried.append(template_service)
        template = registry.find_template(category, template_service)
        if template is not None:
            return template
    # 非エンジニアの営業担当者がそのままエラーメッセージを目にする可能性があるため、
    # 日本語かつ「次に何をすればよいか」が分かる文言にする
    # （obasan-qualityレビュー: reprを含む技術的すぎるメッセージだったとの指摘を反映）。
    raise TemplateNotFoundError(
        f"提案サービス{proposed_services}は書類自動生成（{category}）に対応していません。"
        "テンプレート管理表への登録が必要な場合は情報システム担当に連絡してください。"
    )
