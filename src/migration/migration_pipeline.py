"""kintone CSV → Notion 6DB への一括インポート・パイプライン（T-11、04節末尾の移行手順）。

移行手順：①旧プロパティの取捨選別 → ②新DBプロパティ定義 → ③外部ID（kintone_ID/Zoho_ID）
をキーにした一括インポート → ④リレーションの自動結合 → ⑤名寄せ結果の目視検証。

本モジュールは①②④に相当するロジックを担う（実際の変換は既存の `src/migration/*.py` の
transform_* 関数を利用し、ここでは複数CSV間のリレーション解決・IDマッピング登録・
レポート生成のみを行う）。③の一括インポート実行そのもの（Notion API呼び出し）は
`materialize()` が担い、CSV読込・CLI引数処理は `scripts/migrate_data.py` 側の責務とする。

処理は「計画（plan_migration、純粋関数・I/O無し）」と「実行（materialize、I/O有り・
依存注入でテスト可能）」を分離している。これにより、リレーション解決ロジックは
Notion API・IDマッピングストアをモックせずに検証でき、materialize側はモックのみで
API呼び出し回数・冪等性を検証できる。

■ 既知の設計判断・データギャップ（04_項目マッピングに明記が無いため実装者判断で補った点）:
  - kintone アクション管理の取引先名は「顧客名（法人・個人・施設）」列（取引先マスタと同じ
    表記）で、案件管理側の「施設名（会社名）」とは列名が異なる（実データ確認済み。以前は
    案件管理側の列名を誤って流用しており、取引先マスターへのリレーションがほぼ全件解決
    できていなかった）。
  - kintone アクション管理には案件管理側のレコード番号に相当する「案件番号」列が実データ上
    存在しない（実データ確認済み）。そのためアクション管理DBの「案件管理」リレーション、
    および次回アクション日の案件管理側への反映（`extract_next_action_date_for_project`
    経由）は、現状のkintoneエクスポートでは常に未解決のまま（コード上のロジックは
    `project_kintone_id`が空文字列になるため`_note_attempt`自体が呼ばれず、未解決レポートにも
    出てこない静かな機能不全になる点に注意）。案件との紐付けが必要な場合は、取引先名＋
    時系列等で手動突合するか、kintone側のデータ構造の見直しが必要。
  - 案件管理DBの「案件名」・アクション管理DBの「アクション名」はkintone側に対応項目が
    無い/未確認のため、CSVに同名列があればそれを採用し、無ければ取引先名／アクション種別を
    代用する。
  - サービス・商品DBの「課金形態」（必須セレクト）はkintone側にソースが無いため、
    案件管理「サービス（ショット）」・アクション管理「提案サービス」がいずれもショット
    （単発）起点であることから初期値「イニシャルスポット」で作成する。
  - 「担当営業」「担当メンバー」等のUSER型プロパティは、氏名→NotionユーザーIDの対応表が
    まだ無いため本移行スクリプトでは解決しない（Notion側で空欄のまま作成され、手動割当が
    必要）。未設定になった件数・対象レコードは `unresolved_user_properties` として記録し、
    レポートCSVへ出力する。
  - 取引先マスターDBには紐づく案件管理側の営業ステータスから自動集計する
    「【営業部】営業ステータス」ロールアップが既に存在するため、本パイプラインからの
    書き込みは行わない（`kintone_client_master.derive_client_sales_status`は将来
    独立プロパティとして必要になった場合のために残してあるが、現状未使用）。
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar

from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.base import Tool
from src.db_schema.chain import CHAIN_SCHEMA
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.product import PRODUCT_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA
from src.db_schema.registry import get_schema
from src.migration._utils import normalize_date
from src.migration.action_mapping import (
    extract_next_action_date_for_project,
    transform_kintone_action,
)
from src.migration.contact_migration import (
    build_contact_dedup_key,
    dedupe_contacts,
    split_kintone_contacts,
)
from src.migration.kintone_client_master import (
    extract_chain_name,
    transform_client_master,
)
from src.migration.notion_dedupe import ClientMatchIndex, match_existing_client
from src.migration.project_mapping import transform_kintone_project
from src.migration.zoho_action import transform_zoho_action
from src.migration.zoho_chain import transform_zoho_chain
from src.migration.zoho_client_master import transform_zoho_client_master
from src.migration.zoho_contact import transform_zoho_contact
from src.migration.zoho_product import transform_zoho_product
from src.migration.zoho_project import transform_zoho_project
from src.sync_engine.clients._http import ApiError
from src.sync_engine.id_mapping import IdMapping, IdMappingStore

logger = logging.getLogger(__name__)

# 2026-08-12、本番移行実データで判明: Zoho行の自由記述項目（「【Notion】取引先マスター」等）
# に、過去の手動連携作業で埋め込まれた古いNotionページ直リンクが残っていることがある
# （extract_notion_page_id()が抽出しリレーションのヒントとして使う）。そのページが既に
# 削除済み・どのインテグレーションにも共有されていない等でアクセス不能な場合、Notion APIは
# create_page()を「HTTP 404: Could not find page with ID: <id>. Make sure the relevant
# pages and databases are shared with your integration "<name>".」で拒否する。この404は
# エラーメッセージに具体的にどのページIDが原因かを含むため、そのIDだけをリレーション値から
# 取り除いて1回だけ再作成を試みる（他の正当なプロパティ・リレーションを巻き添えにしない）。
_INVALID_NOTION_PAGE_ID_RE = re.compile(r"[Cc]ould not find page with ID:\s*([0-9a-fA-F-]{32,36})")


def _extract_invalid_page_id(exc: ApiError) -> str | None:
    if exc.status_code != 404:
        return None
    match = _INVALID_NOTION_PAGE_ID_RE.search(exc.message)
    if not match:
        return None
    return match.group(1).replace("-", "")


def _drop_invalid_page_reference(properties: dict[str, Any], invalid_page_id: str) -> dict[str, Any]:
    """`properties`のリレーション値（リスト）からアクセス不能な`invalid_page_id`だけを除いた
    コピーを返す（他のプロパティ・他のリレーション先はそのまま保持する）。"""
    cleaned: dict[str, Any] = {}
    for name, value in properties.items():
        if isinstance(value, list) and invalid_page_id in value:
            cleaned[name] = [v for v in value if v != invalid_page_id]
        else:
            cleaned[name] = value
    return cleaned

# 実行（materialize）時、リレーション先が必ず先に作成済みとなるよう依存順で並べる。
# client_master はchainを、contactとprojectはclient_masterを、actionはclient_master/
# project/contactを参照するため、この順序を崩すとリレーション解決前に参照してしまう。
_MATERIALIZATION_ORDER: tuple[str, ...] = (
    "chain",
    "client_master",
    "product",
    "contact",
    "project",
    "action",
)

# リレーション未解決率がこの割合を超えたら、リレーションキー列名の推測が実データと
# ずれている可能性が高いとみなし、print_summary先頭で目立つ警告を出す（BLOCKER4/5）。
_UNRESOLVED_RATE_WARNING_THRESHOLD = 0.3

# materialize()の進捗ログを何件ごとに出すか（2026-08-10、obasan-qualityレビューBLOCKER対応。
# 148,000件規模の本番投入で進捗が全く見えず、ハングと正常進行の区別がつかない問題への対応。
# 毎件ログすると148,000行のノイズになるため、粒度を落として一定間隔のみログする）。
_PROGRESS_LOG_INTERVAL = 500


class NotionClientLike(Protocol):
    """migrate_data.pyが必要とするNotionクライアントの最小インターフェース。"""

    def create_page(self, properties: dict[str, Any]) -> str: ...


@dataclass
class PreparedRecord:
    """Notionページ作成前の1レコード（プロパティ値は確定済みだが、リレーション先は
    まだ`PreparedRecord`参照のままで、`notion_key`が確定するmaterialize時に解決される）。
    """

    db_key: str
    kintone_id: str | None
    properties: dict[str, Any]
    notion_key: str | None = None


@dataclass
class UnresolvedRelation:
    """リレーション先が①〜③の結果に見つからなかったケース（表記ゆれ・データ不整合等）。"""

    db_key: str
    kintone_id: str | None
    relation_name: str
    raw_value: str


@dataclass
class UnresolvedUserProperty:
    """USER型必須プロパティ（担当営業／担当メンバー）が、氏名→NotionユーザーIDの対応表が
    無いため未設定のまま作成されるケース（BLOCKER4/5）。件数把握・目視割当作業用に記録する。
    """

    db_key: str
    kintone_id: str | None
    property_name: str
    raw_value: str | None


@dataclass
class DedupeReportEntry:
    """名寄せ（複数レコード→1レコード統合）の目視検証用エントリ。"""

    db_key: str
    dedupe_key: str
    sources: list[dict[str, Any]]
    merged: dict[str, Any]


@dataclass
class NeedsReviewClient:
    """既存Notion取引先マスターとの突合で、会社名は一致（または曖昧に複数一致）したが
    郵便番号の食い違い等により自動確定できず、安全側（新規作成）に倒したケース
    （2026-08-10金沢さん方針: データ欠損より重複の方がマシなため、needs_reviewの場合は
    スキップせず新規作成した上でこのレポートに記録し、後からの人の目での重複調査に委ねる）。
    """

    source: str  # "kintone" or "zoho"
    external_id: str | None
    name: str
    reason: str
    candidate_page_id: str | None


@dataclass
class MigrationPlan:
    prepared: dict[str, list[PreparedRecord]]
    unresolved: list[UnresolvedRelation] = field(default_factory=list)
    unresolved_user_properties: list[UnresolvedUserProperty] = field(default_factory=list)
    dedupe_report: list[DedupeReportEntry] = field(default_factory=list)
    skipped_transform_errors: list[str] = field(default_factory=list)
    needs_review_clients: list[NeedsReviewClient] = field(default_factory=list)
    # (db_key, relation_name) ごとの解決試行回数。未解決率算出用（値が入っている行のみ
    # カウントする。空欄でリレーション自体が存在しない場合は試行にカウントしない）。
    relation_attempts: dict[tuple[str, str], int] = field(default_factory=dict)


@dataclass
class MigrationSummary:
    created: dict[str, int]
    skipped_existing: dict[str, int]


class TitleIdGenerator:
    """DBごとのタイトルID（例: CLI-001, MSA-PJ-001）を連番で発行する。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, db_key: str) -> str:
        self._counters[db_key] = self._counters.get(db_key, 0) + 1
        return f"{get_schema(db_key).id_prefix}{self._counters[db_key]:03d}"


def _record_unresolved(
    unresolved: list[UnresolvedRelation],
    *,
    db_key: str,
    kintone_id: str | None,
    relation_name: str,
    raw_value: str,
) -> None:
    """リレーション未解決を記録し、処理を止めずに警告ログのみ出す（一括インポートが
    1件のデータ不整合で全体停止しないようにするための方針）。
    """
    unresolved.append(UnresolvedRelation(db_key, kintone_id, relation_name, raw_value))
    logger.warning(
        "unresolved relation: db=%s kintone_id=%s relation=%s value=%r",
        db_key,
        kintone_id,
        relation_name,
        raw_value,
    )


def plan_migration(
    client_master_rows: list[dict[str, str]],
    project_rows: list[dict[str, str]],
    action_rows: list[dict[str, str]],
    *,
    existing_client_index: ClientMatchIndex | None = None,
    zoho_client_master_rows: list[dict[str, str]] | None = None,
    zoho_contact_rows: list[dict[str, str]] | None = None,
    zoho_project_rows: list[dict[str, str]] | None = None,
    zoho_action_rows: list[dict[str, str]] | None = None,
    zoho_product_rows: list[dict[str, str]] | None = None,
    zoho_chain_rows: list[dict[str, str]] | None = None,
) -> MigrationPlan:
    """CSV行から、Notion作成前の全レコード（リレーション解決済み）と各種レポートを組み立てる。

    I/O（Notion API・IDマッピングストア）を一切行わない純粋関数。依存順序が
    ①取引先マスター→②チェーン→③連絡先→④案件管理→⑤サービス・商品→⑥アクション管理
    であるため、この順で処理する（kintone分・Zoho分とも、この順で同じ共有状態
    （client_by_name等）へ書き込むため、片方のソースで作成/一致したレコードをもう片方も
    自動的に再利用する＝重複作成を避けられる。2026-08-10、金沢さん方針「kintoneもZohoも
    一気に、確実性重視・データ欠損より重複の方がマシ」により導入）。

    `existing_client_index`（省略可）は、既にNotionへ存在する取引先マスターとの名寄せに
    使う（notion_dedupe.fetch_client_master_snapshots()+build_client_match_index()で
    事前に構築し、呼び出し側から渡す。ここでのNotion API呼び出しは行わない＝純粋関数の
    原則を保つ）。Noneの場合は既存Notionとの突合を行わず、常に新規作成する
    （kintone単独での従来動作と同一）。

    取引先マスター以外（連絡先・案件・アクション・サービス・商品・チェーン）のZohoデータは、
    既存Notionとの突合までは行わず「常に新規作成」とする（金沢さん確認済みの方針:
    取引先ほどの厳密な突合は今回は行わず、重複が疑われるものは実データのレポートで
    可視化するに留める）。
    """
    zoho_client_master_rows = zoho_client_master_rows or []
    zoho_contact_rows = zoho_contact_rows or []
    zoho_project_rows = zoho_project_rows or []
    zoho_action_rows = zoho_action_rows or []
    zoho_product_rows = zoho_product_rows or []
    zoho_chain_rows = zoho_chain_rows or []

    prepared: dict[str, list[PreparedRecord]] = {
        "client_master": [],
        "chain": [],
        "contact": [],
        "project": [],
        "product": [],
        "action": [],
    }
    unresolved: list[UnresolvedRelation] = []
    unresolved_user_properties: list[UnresolvedUserProperty] = []
    dedupe_report: list[DedupeReportEntry] = []
    skipped_transform_errors: list[str] = []
    needs_review_clients: list[NeedsReviewClient] = []
    relation_attempts: dict[tuple[str, str], int] = {}
    ids = TitleIdGenerator()

    def _note_attempt(db_key: str, relation_name: str) -> None:
        """リレーション解決を実際に試みた回数を記録する（未解決率算出の分母、BLOCKER4/5）。"""
        key = (db_key, relation_name)
        relation_attempts[key] = relation_attempts.get(key, 0) + 1

    # === ① 取引先マスター ===================================================
    client_by_name: dict[str, PreparedRecord] = {}
    # Zoho固有の外部ID（zoho データID）→ 解決済みPreparedRecord。案件・アクションの
    # Zoho行が「取引先名.id」で参照してきた際の突合に使う。
    client_by_zoho_id: dict[str, PreparedRecord] = {}
    # 既存Notionページのpage_id → 解決済みPreparedRecord（notion_key未設定・prepared未登録の
    # "参照専用"レコード）。Zoho行に埋め込まれたNotionページ直リンクの解決に使う。
    client_by_existing_page_id: dict[str, PreparedRecord] = {}

    def _resolve_or_create_client(
        props: dict[str, Any], name: str, postal_code: str | None, external_id: str | None, *, source: str
    ) -> PreparedRecord:
        """同一実行内で既に解決済み（kintone/Zohoどちらか片方が先に処理済み）の取引先が
        あればそれを再利用し、無ければ既存Notionとの名寄せを試み、それも無ければ新規作成する
        （2026-08-10、kintone/Zoho両ソースを1回の実行で扱う際の重複作成防止の核心ロジック）。

        needs_review（会社名は一致したが郵便番号が食い違う・複数候補で曖昧等）の場合は、
        誤結合のリスクを避けるため既存ページの再利用はせず安全側の新規作成とし、
        `needs_review_clients`へ記録して後から人が確認できるようにする（金沢さん方針:
        データ欠損より重複の方がマシなため、スキップはせず必ず作成する）。
        """
        if name in client_by_name:
            return client_by_name[name]
        if existing_client_index is not None:
            match_result = match_existing_client(name, postal_code, existing_client_index)
            if match_result.matched is not None and not match_result.needs_review:
                existing_record = PreparedRecord(
                    "client_master",
                    None,
                    {CLIENT_MASTER_SCHEMA.title_property.name: match_result.matched.title},
                    notion_key=match_result.matched.page_id,
                )
                client_by_name[name] = existing_record
                return existing_record
            if match_result.needs_review:
                needs_review_clients.append(
                    NeedsReviewClient(
                        source=source,
                        external_id=external_id,
                        name=name,
                        reason=match_result.reason or "unknown",
                        candidate_page_id=(
                            match_result.matched.page_id if match_result.matched else None
                        ),
                    )
                )
        new_record = PreparedRecord("client_master", external_id, props)
        prepared["client_master"].append(new_record)
        client_by_name[name] = new_record
        return new_record

    def _resolve_client_by_zoho_hint(zoho_id: str | None, notion_page_id: str | None) -> PreparedRecord | None:
        """Zohoの案件・アクション行が持つ「取引先.id」「【Notion】取引先マスター」埋め込み
        リンクから、既に①で解決済みの取引先マスターPreparedRecordを引き当てる。
        ①で作成/一致した取引先とは独立の手がかり（zoho データID／既存Notionページ直リンク）
        のため、専用のインデックス2つ（client_by_zoho_id/client_by_existing_page_id）を
        別途参照する。"""
        if zoho_id and zoho_id in client_by_zoho_id:
            return client_by_zoho_id[zoho_id]
        if notion_page_id:
            if notion_page_id not in client_by_existing_page_id:
                client_by_existing_page_id[notion_page_id] = PreparedRecord(
                    "client_master", None, {}, notion_key=notion_page_id
                )
            return client_by_existing_page_id[notion_page_id]
        return None

    # 行ごとに解決済みPreparedRecordを記録する（②チェーンのリレーション付けで
    # `client_master_rows`と位置対応させて参照する。かつては`prepared["client_master"]`が
    # 各行と1:1対応することを前提にzip()していたが、既存Notionと一致した行は
    # `prepared["client_master"]`に追加されなくなった（新規作成しないため）ため、
    # 位置対応が崩れてしまう。専用のリストで対応関係を明示的に保持する。
    client_records_by_row: list[PreparedRecord] = []
    for row in client_master_rows:
        props = transform_client_master(row)
        # BLOCKER: 以前はここで props[title_property.name]（="取引先名"）を ids.next(...)
        # の連番IDで上書きしており、transform_client_master()が既にセットした実際の会社名が
        # 失われ、Notion上のタイトルが全件"CLI-001"のような連番IDになってしまうバグが
        # あった（実データ検証で発覚）。取引先名は既にtransform_client_master()の戻り値に
        # 含まれているため、ここでの追加代入は不要かつ有害だった。
        # BLOCKER: "kintone_ID"はNotion側に存在しないプロパティ名（CLIENT_MASTER_SCHEMAには
        # 定義が無い）。PreparedRecord.propertiesにそのまま残していると、実際の
        # build_notion_properties()呼び出し時（materialize()のdry_run=Falseパス）に
        # 確実にKeyErrorで失敗する。IDマッピング用の外部IDとしてのみ使い、
        # Notion書き込み対象のprops辞書からは取り除く（--dry-runではこの経路を通らず
        # 検知できないバグだったため実データ検証でも見つからなかった）。
        kintone_id = props.pop("kintone_ID") or None
        name = props["取引先名"]
        if name and name in client_by_name:
            # 同名取引先がkintone CSV内に複数行ある場合、以前からの既存動作（各行を
            # そのまま新規作成し、最初の行のみをリレーション解決の正とする）を維持する
            # （Q-08の名寄せ対象はあくまで連絡先。取引先自体の同一ソース内名寄せは対象外の
            # 従来方針。ここを変えるとkintone単独運用時の挙動が変わってしまうため、
            # 今回のkintone/Zoho統合では触れない）。同名重複はデータ不整合の可能性があるため
            # 気づけるよう警告ログを残す（WARN10）。
            logger.warning(
                "duplicate 取引先名 detected: %r (kintone_id=%s). "
                "only the first occurrence (kintone_id=%s) is used for relation resolution",
                name,
                kintone_id,
                client_by_name[name].kintone_id,
            )
            record = PreparedRecord("client_master", kintone_id, props)
            prepared["client_master"].append(record)
            client_records_by_row.append(record)
            continue
        if not name:
            record = PreparedRecord("client_master", kintone_id, props)
            prepared["client_master"].append(record)
            client_records_by_row.append(record)
            continue
        client_records_by_row.append(
            _resolve_or_create_client(props, name, props.get("郵便番号"), kintone_id, source="kintone")
        )

    # === ① 取引先マスター（Zoho） ============================================
    # kintoneと異なり、Zoho側はCSV内の同名重複も含めて①の共有レジストリ（client_by_name）で
    # 名寄せする（Zohoは新規統合のため、kintoneのような温存すべき既存動作が無いため）。
    for row in zoho_client_master_rows:
        props = transform_zoho_client_master(row)
        zoho_id = props.pop("zoho_ID") or None
        name = props["取引先名"]
        if not name:
            record = PreparedRecord("client_master", zoho_id, props)
            prepared["client_master"].append(record)
            continue
        record = _resolve_or_create_client(props, name, props.get("郵便番号"), zoho_id, source="zoho")
        if zoho_id:
            client_by_zoho_id[zoho_id] = record

    # === ② チェーン =========================================================
    chain_by_name: dict[str, PreparedRecord] = {}
    for row in client_master_rows:
        chain_name = extract_chain_name(row)
        if chain_name is None or chain_name in chain_by_name:
            continue
        # BLOCKER: 以前は存在しない"チェーン名"キーへも書き込んでおり（CHAIN_SCHEMAの
        # titleプロパティは"グループ名"のみで"チェーン名"というプロパティは無い）、実書き込み
        # 時に確実にKeyErrorで失敗するバグがあった（"グループ名"キーで後から上書きされる
        # dict重複キーの挙動でtitle自体は正しく設定されていたため、titleの上書き調査だけでは
        # 見つからず、schema.get_property()による全キー検証の回帰テストで発覚）。
        chain_props: dict[str, Any] = {
            # kintone取引先マスタに「グループ名」に相当する個別項目が無いため、
            # チェーン名をそのまま流用する。
            CHAIN_SCHEMA.title_property.name: chain_name,
        }
        chain_record = PreparedRecord("chain", f"chain:{chain_name}", chain_props)
        prepared["chain"].append(chain_record)
        chain_by_name[chain_name] = chain_record

    for row, client_record in zip(client_master_rows, client_records_by_row):
        chain_name = extract_chain_name(row)
        if chain_name is None:
            continue
        _note_attempt("client_master", "チェーン")
        chain_record = chain_by_name.get(chain_name)
        if chain_record is None:
            _record_unresolved(
                unresolved,
                db_key="client_master",
                kintone_id=client_record.kintone_id,
                relation_name="チェーン",
                raw_value=chain_name,
            )
            continue
        client_record.properties["チェーン"] = [chain_record]

    # === ② チェーン（Zoho） ==================================================
    # kintone由来のチェーン（取引先マスタ「本部名」から抽出した簡易的なもの）と同じ
    # chain_by_nameを共有し、名前が一致すれば重複作成しない。
    for row in zoho_chain_rows:
        chain_props = transform_zoho_chain(row)
        zoho_chain_id = chain_props.pop("zoho_ID") or None
        chain_name = chain_props["グループ名"]
        if not chain_name or chain_name in chain_by_name:
            continue
        chain_record = PreparedRecord("chain", zoho_chain_id, chain_props)
        prepared["chain"].append(chain_record)
        chain_by_name[chain_name] = chain_record

    # === ③ 連絡先 ============================================================
    raw_contacts: list[dict[str, str | None]] = []
    for row in client_master_rows:
        raw_contacts.extend(split_kintone_contacts(row))

    contact_groups: dict[str, list[dict[str, str | None]]] = {}
    contact_order: list[str] = []
    for contact in raw_contacts:
        key = build_contact_dedup_key(contact)
        if key not in contact_groups:
            contact_order.append(key)
        contact_groups.setdefault(key, []).append(contact)

    contact_by_name_and_client: dict[tuple[str, str], PreparedRecord] = {}
    contact_by_name: dict[str, PreparedRecord] = {}
    for key in contact_order:
        group = contact_groups[key]
        merged = dedupe_contacts(group)[0]
        client_name = merged.get("取引先名") or ""
        _note_attempt("contact", "取引先マスター")
        client_record = client_by_name.get(client_name)
        if client_record is None:
            _record_unresolved(
                unresolved,
                db_key="contact",
                kintone_id=merged.get("kintone_client_id"),
                relation_name="取引先マスター",
                raw_value=client_name,
            )

        # BLOCKER: CONTACT_SCHEMAのdocstringに明記の通り、Notion側のtitleプロパティ
        # （"名前"）が氏名そのものを保持する設計であり、"氏名"という別プロパティは存在
        # しない。以前はtitleに連番IDを入れ、存在しない"氏名"キーへ実際の氏名を書き込もう
        # としており、Notion書き込み時にKeyErrorで確実に失敗していた（実データ検証で発覚）。
        props: dict[str, Any] = {
            CONTACT_SCHEMA.title_property.name: merged["氏名"],
            "部署": merged.get("部署"),
            "役職": merged.get("役職"),
            "携帯番号": merged.get("携帯番号"),
            "メールアドレス": merged.get("メールアドレス"),
            "取引先マスター": [client_record] if client_record else [],
        }
        # 連絡先DB（CONTACT_SCHEMA）はkintone側の外部IDプロパティを持たない（横持ち項目の
        # 分割・名寄せ後の新規独立DBのため）。IDマッピングストアの冪等性チェック用に、
        # 名寄せキーを流用した合成キーを充てる（Notion側へは送らない内部値）。
        record = PreparedRecord("contact", f"contact:{key}", props)
        prepared["contact"].append(record)
        if client_name:
            contact_by_name_and_client[(merged["氏名"], client_name)] = record
        contact_by_name.setdefault(merged["氏名"], record)

        if len(group) > 1:
            dedupe_report.append(
                DedupeReportEntry(
                    db_key="contact",
                    dedupe_key=key,
                    sources=[dict(c) for c in group],
                    merged=dict(merged),
                )
            )

    # === ③ 連絡先（Zoho） ====================================================
    # 取引先マスターへのリレーションは①で共有したclient_by_nameの完全一致のみで解決する
    # （連絡先・案件・アクションはZoho側で厳密な突合までは行わない方針、金沢さん確認済み）。
    for row in zoho_contact_rows:
        contact_props = transform_zoho_contact(row)
        zoho_contact_id = contact_props.pop("zoho_ID") or None
        company_name = contact_props.pop("_会社名") or ""
        contact_name = contact_props["名前"]
        if not contact_name:
            continue
        _note_attempt("contact", "取引先マスター")
        client_record = client_by_name.get(company_name) if company_name else None
        if client_record is None and company_name:
            _record_unresolved(
                unresolved,
                db_key="contact",
                kintone_id=zoho_contact_id,
                relation_name="取引先マスター",
                raw_value=company_name,
            )
        contact_props["取引先マスター"] = [client_record] if client_record else []
        contact_record = PreparedRecord("contact", zoho_contact_id, contact_props)
        prepared["contact"].append(contact_record)
        contact_by_name.setdefault(contact_name, contact_record)
        if company_name:
            contact_by_name_and_client.setdefault((contact_name, company_name), contact_record)

    # === ④ 案件管理 & ⑤ サービス・商品 ========================================
    product_by_name: dict[str, PreparedRecord] = {}

    def ensure_product(name: str) -> PreparedRecord:
        existing = product_by_name.get(name)
        if existing is not None:
            return existing
        # BLOCKER: PRODUCT_SCHEMAのdocstringに明記の通り、Notion側のtitleプロパティ
        # （"名前"）がサービス名そのものを保持する設計であり、"サービス名"という別プロパティ
        # は存在しない。以前はtitleに連番IDを入れ、存在しない"サービス名"キーへ実際の値を
        # 書き込もうとしており、Notion書き込み時にKeyErrorで確実に失敗していた
        # （実データ検証で発覚）。
        product_props: dict[str, Any] = {
            PRODUCT_SCHEMA.title_property.name: name,
            # 案件管理「サービス（ショット）」・アクション管理「提案サービス」はいずれも
            # ショット（単発）提案起点の項目で、月額/成果報酬を判別するソースが無いため、
            # 初期値は「イニシャルスポット」とする（実データ精査後に手動調整する前提）。
            "課金形態": "イニシャルスポット",
        }
        new_record = PreparedRecord("product", f"product:{name}", product_props)
        prepared["product"].append(new_record)
        product_by_name[name] = new_record
        return new_record

    # === ⑤ サービス・商品（Zoho） ============================================
    # Zohoには実際のサービス・商品マスタ（サービス・商品_001.csv）が存在するため、
    # ensure_product()（kintone側の案件・アクションから拾った名前のみの簡易登録）とは別に、
    # 実データ（初期費用・月額費用込み）をそのまま登録する。同名の場合は
    # ensure_product/kintone側どちらが先でも重複させない。
    for row in zoho_product_rows:
        zoho_product_props = transform_zoho_product(row)
        zoho_product_id = zoho_product_props.pop("zoho_ID") or None
        zoho_product_name = zoho_product_props["名前"]
        if not zoho_product_name or zoho_product_name in product_by_name:
            continue
        zoho_product_record = PreparedRecord("product", zoho_product_id, zoho_product_props)
        prepared["product"].append(zoho_product_record)
        product_by_name[zoho_product_name] = zoho_product_record

    project_by_kintone_id: dict[str, PreparedRecord] = {}
    for row in project_rows:
        try:
            transformed = transform_kintone_project(row)
        except ValueError as exc:
            msg = f"案件管理レコード {row.get('レコード番号')!r} をスキップ: {exc}"
            logger.warning(msg)
            skipped_transform_errors.append(msg)
            continue

        kintone_id = transformed["kintone_ID"] or None
        client_name = transformed["_取引先名"]
        _note_attempt("project", "取引先マスター")
        client_record = client_by_name.get(client_name) if client_name else None
        if client_record is None:
            _record_unresolved(
                unresolved,
                db_key="project",
                kintone_id=kintone_id,
                relation_name="取引先マスター",
                raw_value=client_name or "",
            )
        service_records = [
            ensure_product(name) for name in dict.fromkeys(transformed["_サービス名リスト"])
        ]

        props = {
            PROJECT_SCHEMA.title_property.name: ids.next("project"),
            # kintone案件管理に「案件名」専用の項目が確認できないため、CSVに同名列があれば
            # それを採用し、無ければ取引先名で代用する（1社=1案件が主だった想定の簡易対応）。
            "案件名": row.get("案件名") or client_name or "",
            "取引先マスター": [client_record] if client_record else [],
            "営業ステータス": transformed["営業ステータス"],
            "提案サービス": service_records,
            "初期費用": transformed["初期費用"],
            "月額費用": transformed["月額費用"],
            "契約日 / 予想契約日": transformed["契約日 / 予想契約日"],
        }
        # NOTE: PROJECT_SCHEMAには"kintone_ID"に相当するプロパティが定義されていないため
        # (CLIENT_MASTER_SCHEMA/ACTION_SCHEMAとは異なる)、Notionプロパティとしては送らず
        # PreparedRecord.kintone_id（IDマッピング専用）としてのみ保持する。
        project_record = PreparedRecord("project", kintone_id, props)
        prepared["project"].append(project_record)
        if kintone_id:
            project_by_kintone_id[kintone_id] = project_record
        # 担当メンバー（USER型・必須）はkintone案件管理側に対応する列が確認できないため、
        # 本移行スクリプトでは解決しない。全件が未設定になる点をレポートで可視化する
        # （BLOCKER4/5）。
        unresolved_user_properties.append(
            UnresolvedUserProperty(
                db_key="project",
                kintone_id=kintone_id,
                property_name="担当メンバー",
                raw_value=None,
            )
        )

    # NOTE: ①取引先マスターDBには、紐づく案件の営業ステータスから自動集計する
    # 「【営業部】営業ステータス」が既にロールアップ（読み取り専用）として存在するため、
    # 同じ目的の値を独立プロパティへ重複して書き込む処理は行わない（2026-08-09、
    # 業務判断確認済み。旧実装は"営業ステータス"という存在しないプロパティ名で書き込もう
    # としており、実行時に確実にKeyErrorで失敗するバグがあった）。

    # 案件へのNotionページ直リンク参照専用インデックス（Zohoアクションの「案件名」列に
    # 埋め込まれたNotion案件管理ページへの直リンク解決に使う。①の
    # client_by_existing_page_idと同じ考え方）。
    project_by_existing_page_id: dict[str, PreparedRecord] = {}

    def _resolve_project_by_notion_hint(notion_page_id: str | None) -> PreparedRecord | None:
        if not notion_page_id:
            return None
        if notion_page_id not in project_by_existing_page_id:
            project_by_existing_page_id[notion_page_id] = PreparedRecord(
                "project", None, {}, notion_key=notion_page_id
            )
        return project_by_existing_page_id[notion_page_id]

    # === ④ 案件管理（Zoho） ==================================================
    # 実データ確認済み(2026-08-10): PROJECT_SCHEMAの多くのプロパティ名がZoho側とほぼ
    # 1対1で一致するカスタム構築のため、transform_zoho_project()の戻り値をそのまま
    # ベースに使い、リレーション（取引先マスター・提案サービス）のみここで解決する。
    # 「案件名」（titleプロパティ）はkintoneと異なりZoho側に実データがあるため、
    # ids.next()による連番上書きは行わない（transform_zoho_project()が既に実際の
    # 案件名をセットしている）。
    for row in zoho_project_rows:
        transformed = transform_zoho_project(row)
        zoho_project_id = transformed.pop("zoho_ID") or None
        client_zoho_id = transformed.pop("_取引先_zoho_id")
        client_notion_page_id = transformed.pop("_取引先_notion_page_id")
        service_names = transformed.pop("_サービス名リスト")

        _note_attempt("project", "取引先マスター")
        client_record = _resolve_client_by_zoho_hint(client_zoho_id, client_notion_page_id)
        if client_record is None:
            _record_unresolved(
                unresolved,
                db_key="project",
                kintone_id=zoho_project_id,
                relation_name="取引先マスター",
                raw_value=client_zoho_id or client_notion_page_id or "",
            )
        transformed["取引先マスター"] = [client_record] if client_record else []
        transformed["提案サービス"] = [ensure_product(name) for name in dict.fromkeys(service_names)]

        zoho_project_record = PreparedRecord("project", zoho_project_id, transformed)
        prepared["project"].append(zoho_project_record)
        unresolved_user_properties.append(
            UnresolvedUserProperty(
                db_key="project",
                kintone_id=zoho_project_id,
                property_name="担当メンバー",
                raw_value=None,
            )
        )

    # === ⑥ アクション管理 =====================================================
    for row in action_rows:
        try:
            transformed = transform_kintone_action(row)
        except ValueError as exc:
            msg = f"アクション管理レコード {row.get('レコード番号')!r} をスキップ: {exc}"
            logger.warning(msg)
            skipped_transform_errors.append(msg)
            continue

        kintone_act_id = transformed["kintone_Act_ID"] or None

        # 実データ確認済み: 取引先の会社名を表すkintoneの列名はアプリごとに表記が異なる
        # （取引先マスタ・アクション管理＝「顧客名（法人・個人・施設）」、案件管理＝
        # 「施設名（会社名）」）。以前は案件管理側の列名をそのまま流用しており、
        # アクション管理の取引先マスターへのリレーションがほぼ全件解決できなかった。
        client_name = (row.get("顧客名（法人・個人・施設）") or "").strip()
        _note_attempt("action", "取引先マスター")
        client_record = client_by_name.get(client_name) if client_name else None
        if client_record is None:
            _record_unresolved(
                unresolved,
                db_key="action",
                kintone_id=kintone_act_id,
                relation_name="取引先マスター",
                raw_value=client_name,
            )

        project_kintone_id = (row.get("案件番号") or "").strip()
        project_record = project_by_kintone_id.get(project_kintone_id) if project_kintone_id else None
        if project_kintone_id:
            _note_attempt("action", "案件管理")
        if project_kintone_id and project_record is None:
            _record_unresolved(
                unresolved,
                db_key="action",
                kintone_id=kintone_act_id,
                relation_name="案件管理",
                raw_value=project_kintone_id,
            )

        contact_name = transformed["_先方担当者氏名"]
        contact_record = None
        if contact_name:
            _note_attempt("action", "先方担当者")
            contact_record = contact_by_name_and_client.get((contact_name, client_name))
            if contact_record is None:
                contact_record = contact_by_name.get(contact_name)
                if contact_record is not None:
                    # (氏名, 取引先名) では見つからず、氏名のみで別取引先の連絡先へ
                    # フォールバック解決したケース。同姓同名の別取引先担当者を誤って
                    # 結合している可能性があるため、無言にせず警告ログへ残す（WARN7）。
                    logger.warning(
                        "action kintone_id=%s: 先方担当者 %r は取引先 %r 内では見つからず、"
                        "氏名のみで別取引先の連絡先へフォールバック解決しました"
                        "（誤結合の可能性があるため要確認）",
                        kintone_act_id,
                        contact_name,
                        client_name,
                    )
            if contact_record is None:
                _record_unresolved(
                    unresolved,
                    db_key="action",
                    kintone_id=kintone_act_id,
                    relation_name="先方担当者",
                    raw_value=contact_name,
                )

        # ACTION_SCHEMAには提案サービスのリレーション項目が存在しないため、サービス・商品DB
        # への新規登録（重複除去）のみ行い、アクション側プロパティへは反映しない。
        for name in transformed["_提案サービス名リスト"]:
            ensure_product(name)

        next_action_date = extract_next_action_date_for_project(row)
        if next_action_date and project_record is not None:
            project_record.properties["次回アクション日"] = next_action_date

        # BLOCKER: 以下3件は以前、実在しないプロパティ名/誤った値の型で書き込もうとしており
        # 実書き込み時に確実にKeyError（またはbuild_notion_property_valueでの型不一致）に
        # なるバグだった（schema.get_property()による全キー検証の回帰テストで発覚）。
        # - "取引先マスター" → ACTION_SCHEMAでの実際のプロパティ名は
        #   "👨‍👩‍👧‍👦 取引先マスター"（絵文字プレフィックス付き）。
        # - "案件管理" → ACTION_SCHEMAには存在せず、実際のプロパティ名は"案件名"
        #   （名前はtitleっぽく紛らわしいが実体はrelation。ACTION_SCHEMAのtitleは
        #   別途「商談回数・電話回数・メール回数（何回目）」）。
        # - "先方担当者" → プロパティ自体はキー名として存在するが、ACTION_SCHEMA上は
        #   RELATIONではなくTEXT型（自由記述、連絡先DBへの正式なリレーションは無い設計）。
        #   contact_recordの解決結果はリレーションlistではなく素のテキスト名として書き込む
        #   （contact_record自体は解決成否のログ・未解決レポート用に引き続き算出している）。
        action_props = {
            ACTION_SCHEMA.title_property.name: ids.next("action"),
            "アクション種別": transformed["アクション種別"],
            "アクション日": normalize_date(row.get("アクション日")),
            "👨‍👩‍👧‍👦 取引先マスター": [client_record] if client_record else [],
            "案件名": [project_record] if project_record else [],
            "先方担当者": contact_name or None,
            "履歴メモ": transformed["履歴メモ"],
        }
        # BLOCKER: "kintone_Act_ID"もclient_masterの"kintone_ID"と同種のバグで、
        # ACTION_SCHEMAに存在しないプロパティ名のため実書き込み時に確実にKeyErrorになる。
        # 上のkintone_act_id変数（PreparedRecordの外部IDとしてのみ使用）で足りており、
        # action_propsへ重複して含める必要は無い。
        prepared["action"].append(PreparedRecord("action", kintone_act_id, action_props))
        # 担当営業（USER型・必須）は氏名→NotionユーザーIDの対応表がまだ無いため解決しない。
        # kintone「対応者」の氏名だけは`_担当営業氏名`として抽出済みなので、手動割当作業の
        # 手掛かりとしてレポートへ残す（WARN4/BLOCKER4/5）。
        unresolved_user_properties.append(
            UnresolvedUserProperty(
                db_key="action",
                kintone_id=kintone_act_id,
                property_name="担当営業",
                raw_value=transformed["_担当営業氏名"] or None,
            )
        )

    # === ⑥ アクション管理（Zoho） =============================================
    # 実データ確認済み(2026-08-10): 取引先へのリレーションはZoho内部ID（「取引先.id」）と
    # 過去の連携作業で埋め込まれたNotionページ直リンク（「【Notion】取引先マスター」）を
    # 合わせて93.9%が解決できる（①で構築したclient_by_zoho_id/client_by_existing_page_id
    # を介して解決）。案件へのリレーションは埋め込みNotionページ直リンクのみ（10.0%）。
    # 先方担当者はACTION_SCHEMA上TEXT型（正式なリレーションが無い設計）のため、
    # transform_zoho_action()が返す文字列をそのまま使う（追加の解決処理は不要）。
    for row in zoho_action_rows:
        transformed = transform_zoho_action(row)
        zoho_act_id = transformed.pop("zoho_Act_ID") or None
        client_zoho_id = transformed.pop("_取引先_zoho_id")
        client_notion_page_id = transformed.pop("_取引先_notion_page_id")
        project_notion_page_id = transformed.pop("_案件_notion_page_id")

        _note_attempt("action", "取引先マスター")
        zoho_client_record = _resolve_client_by_zoho_hint(client_zoho_id, client_notion_page_id)
        if zoho_client_record is None:
            _record_unresolved(
                unresolved,
                db_key="action",
                kintone_id=zoho_act_id,
                relation_name="取引先マスター",
                raw_value=client_zoho_id or client_notion_page_id or "",
            )

        if project_notion_page_id:
            _note_attempt("action", "案件管理")
        zoho_project_ref = _resolve_project_by_notion_hint(project_notion_page_id)

        transformed["👨‍👩‍👧‍👦 取引先マスター"] = [zoho_client_record] if zoho_client_record else []
        transformed["案件名"] = [zoho_project_ref] if zoho_project_ref else []

        prepared["action"].append(PreparedRecord("action", zoho_act_id, transformed))
        # 担当営業（USER型・必須）はZoho「アクションの担当者.id」→Notionユーザーの対応表が
        # まだ無いため解決しない（kintoneと同様の既知の制約）。
        unresolved_user_properties.append(
            UnresolvedUserProperty(
                db_key="action",
                kintone_id=zoho_act_id,
                property_name="担当営業",
                raw_value=None,
            )
        )

    return MigrationPlan(
        prepared=prepared,
        unresolved=unresolved,
        unresolved_user_properties=unresolved_user_properties,
        needs_review_clients=needs_review_clients,
        dedupe_report=dedupe_report,
        skipped_transform_errors=skipped_transform_errors,
        relation_attempts=relation_attempts,
    )


def business_id(record: PreparedRecord) -> str:
    """レコードのtitleプロパティ値（DBによって連番ID/取引先名/氏名/サービス名等）を返す。
    dry-run時のnotion_keyプレースホルダ、およびレポート表示に使う。
    """
    title_name = get_schema(record.db_key).title_property.name
    return str(record.properties[title_name])


def resolved_properties(record: PreparedRecord) -> dict[str, Any]:
    """`PreparedRecord`参照を含むリレーション値を、確定済みの`notion_key`へ変換する。

    materialize()の依存順処理により、参照先レコードは必ず先に処理済み
    （`notion_key`が設定済み）である前提。
    """
    resolved: dict[str, Any] = {}
    for prop_name, value in record.properties.items():
        if isinstance(value, list) and (not value or isinstance(value[0], PreparedRecord)):
            resolved[prop_name] = [target.notion_key for target in value]
        else:
            resolved[prop_name] = value
    return resolved


def materialize(
    plan: MigrationPlan,
    *,
    id_mapping_store: IdMappingStore | None,
    notion_clients: Mapping[str, NotionClientLike] | None,
    dry_run: bool,
    notion_client_pools: Mapping[str, Sequence[NotionClientLike]] | None = None,
) -> MigrationSummary:
    """計画済みレコードを実際にNotionへ作成し、IDマッピングストアへ登録する。

    dry_run=True の場合、Notion API・IDマッピングストアへは一切アクセスしない
    （引数に何を渡していても呼び出さない）。`notion_client_pools`を渡していても
    dry_run=True時は完全に無視される（並列実行のテスト目的でdry_run=Trueと
    notion_client_poolsを組み合わせても意味を持たない）。

    `notion_client_pools`（省略可）にdb_keyを指定すると、そのdb_keyのレコード作成を
    複数のNotionClientLike（別々のAPIトークンを持つ複数のNotionインテグレーション）を
    使ってスレッドプールで並列実行する（2026-08-10、Notion本番一括投入（148,000件規模・
    単一トークンでは10時間超）を短縮するために追加。Notionのレート制限は1インテグレーション
    あたり平均秒3リクエスト程度のため、複数の別インテグレーションを対象DB全てに共有すれば
    実質的な上限を線形に引き上げられる）。指定されなかったdb_keyは従来通り
    `notion_clients[db_key]` を使った逐次実行のまま。並列化するのは同一db_key内の
    レコード同士のみで、db_key間の依存順序（_MATERIALIZATION_ORDER）は従来通り守る
    （後続db_keyのレコードは先行db_keyの`record.notion_key`確定を前提に
    リレーション解決するため）。
    """
    created = {key: 0 for key in _MATERIALIZATION_ORDER}
    skipped_existing = {key: 0 for key in _MATERIALIZATION_ORDER}
    # created/skipped_existingへの書き込みとid_mapping_storeへのアクセス（SQLite、
    # 複数スレッドからの同時書き込みは想定されていない）を、並列実行時のみロックで直列化する。
    counts_lock = threading.Lock()
    store_lock = threading.Lock()

    # shirokuma-secレビューBLOCKER対応: kintone_idごとの排他ロック。同一kintone_idを持つ
    # 2レコードが並列実行され、「存在チェック（find_by_external_id）」と「作成+登録
    # （create_page+upsert）」が別々のロック区間に分かれていたため、両方が同時に
    # 「未登録」と判定してしまい、(a)Notion側に孤児ページが2件作成され、(b)2件目の
    # upsert()がUNIQUE制約違反（DuplicateExternalIdError）でmaterialize()全体を
    # クラッシュさせる、というTOCTOU（check-then-act）競合が実際に再現された。
    # kintone_idごとにロックし、同一idの処理だけを直列化することで、異なるid同士の並列性は
    # 保ったまま解消する（辞書へのロックオブジェクト登録自体はid_locks_guardで保護する）。
    id_locks: dict[str, threading.Lock] = {}
    id_locks_guard = threading.Lock()

    def _lock_for_kintone_id(kintone_id: str) -> threading.Lock:
        with id_locks_guard:
            lock = id_locks.get(kintone_id)
            if lock is None:
                lock = threading.Lock()
                id_locks[kintone_id] = lock
            return lock

    def _maybe_log_progress(db_key: str, total: int) -> None:
        """obasan-qualityレビューBLOCKER対応: 148,000件規模・数時間の無人実行で、
        進捗ログが起動時の1行しか出ずハングと正常進行の区別がつかなかった問題への対応。
        counts_lock保持中に呼ぶ前提（doneの読み取りとログ出力の間で値がずれないように）。
        """
        done = created[db_key] + skipped_existing[db_key]
        if done % _PROGRESS_LOG_INTERVAL == 0 or done == total:
            logger.info(
                "[%s] %d/%d件処理済み（作成%d件・既存スキップ%d件）",
                db_key,
                done,
                total,
                created[db_key],
                skipped_existing[db_key],
            )

    def _check_create_and_register(
        db_key: str, record: PreparedRecord, client: NotionClientLike, *, total: int, client_label: str
    ) -> None:
        with store_lock:
            existing = (
                id_mapping_store.find_by_external_id(Tool.KINTONE, record.kintone_id, db_key=db_key)
                if record.kintone_id
                else None
            )
        if existing is not None:
            # 一度Notionへ移行済みのレコードは、以後Notionを正とする運用方針
            # （05_同期・競合制御「Notion優先」原則）に合わせ、再実行時は上書きせず
            # スキップする（このバッチが手動修正結果を巻き戻さないようにするため）。
            record.notion_key = existing.notion_key
            with counts_lock:
                skipped_existing[db_key] += 1
                _maybe_log_progress(db_key, total)
            return

        try:
            page_id = client.create_page(resolved_properties(record))
        except ApiError as exc:
            invalid_page_id = _extract_invalid_page_id(exc)
            if invalid_page_id is None:
                logger.error("[%s] %s でのcreate_page()が失敗しました", db_key, client_label)
                raise
            # 過去の手動連携作業で埋め込まれた古いNotionページ直リンクがアクセス不能だった
            # ケース（詳細はモジュール冒頭のコメント参照）。そのページIDだけをリレーションから
            # 除いて1回だけ再作成を試みる。
            logger.warning(
                "[%s] kintone_id=%s: アクセス不能なページ参照（%s）が含まれていたため、"
                "そのリレーションを除いて再作成を試みます",
                db_key,
                record.kintone_id,
                invalid_page_id,
            )
            cleaned_properties = _drop_invalid_page_reference(
                resolved_properties(record), invalid_page_id
            )
            try:
                page_id = client.create_page(cleaned_properties)
            except Exception:
                logger.error(
                    "[%s] %s でのcreate_page()が失敗しました（アクセス不能なページ参照を"
                    "除いた再試行後も失敗）",
                    db_key,
                    client_label,
                )
                raise
        except Exception:
            # obasan-qualityレビューWARN対応: 複数トークンを並列で使う運用では、DB共有忘れ等の
            # 設定ミスで特定のトークンだけが失敗するケースがあり得る。プール中の何番目の
            # クライアント（=何番目のAPIキー）が原因かをログに残し、原因切り分けの初手を
            # 「6トークンを1つずつ疑う」作業にしないようにする。
            logger.error("[%s] %s でのcreate_page()が失敗しました", db_key, client_label)
            raise
        record.notion_key = page_id
        with store_lock:
            id_mapping_store.upsert(
                IdMapping(notion_key=page_id, db_key=db_key, kintone_id=record.kintone_id)
            )
        with counts_lock:
            created[db_key] += 1
            _maybe_log_progress(db_key, total)

    def _materialize_one(
        db_key: str, record: PreparedRecord, client: NotionClientLike, *, total: int, client_label: str
    ) -> None:
        assert id_mapping_store is not None
        # 「存在チェック→作成→登録」全体をkintone_id単位で直列化する（上のBLOCKER対応
        # コメント参照）。kintone_idが無いレコードはそもそも名寄せ判定自体を行わない
        # （そのようなレコードは複数存在しても重複判定の対象外＝従来通り全件作成される）
        # ため、ロック無しで進める。
        if record.kintone_id:
            with _lock_for_kintone_id(record.kintone_id):
                _check_create_and_register(db_key, record, client, total=total, client_label=client_label)
        else:
            _check_create_and_register(db_key, record, client, total=total, client_label=client_label)

    for db_key in _MATERIALIZATION_ORDER:
        records = plan.prepared[db_key]

        if dry_run:
            for record in records:
                record.notion_key = business_id(record)
                created[db_key] += 1
            continue

        if not records:
            # 対象レコードが無いdb_keyについては、従来通りid_mapping_store/notion_clientsの
            # 必須チェックも含めて一切アクセスしない（この db_key 分のクライアントを
            # 呼び出し側が用意していなくても問題にならない、という既存動作を維持する）。
            continue

        if id_mapping_store is None or notion_clients is None:
            raise ValueError("dry_run=False の場合、id_mapping_store/notion_clients は必須です")

        total = len(records)
        logger.info("[%s] %d件の書き込みを開始します", db_key, total)

        pool = notion_client_pools.get(db_key) if notion_client_pools else None
        if pool:
            executor = ThreadPoolExecutor(max_workers=len(pool))
            futures = [
                executor.submit(
                    _materialize_one,
                    db_key,
                    record,
                    pool[i % len(pool)],
                    total=total,
                    client_label=f"トークン{i % len(pool) + 1}/{len(pool)}",
                )
                for i, record in enumerate(records)
            ]
            try:
                # shirokuma-secレビューWARN対応: 以前は`future.result()`をsubmit順に
                # 呼んでいたため、最初に見つかった失敗の例外だけが送出され、それより後ろの
                # futureで発生した別の失敗は`.result()`が一度も呼ばれず無言で握りつぶされて
                # いた（実際に複数件同時失敗のシナリオで再現された）。全futureの完了を待って
                # `.exception()`で結果を集め、失敗が複数あっても全件ログに残す。
                errors: list[BaseException] = []
                for future in futures:
                    exc = future.exception()
                    if exc is not None:
                        errors.append(exc)
            except BaseException:
                # obasan-qualityレビューBLOCKER対応: KeyboardInterrupt等での中断時、
                # cancel_futures=Trueでまだ着手していないFutureを即座にキャンセルし、
                # 数万件規模のキューが残っていても長時間ブロックしないようにする。
                # wait=Trueは維持する: wait=Falseにすると、既に実行中のワーカースレッド
                # （最大でもワーカー数分程度）がid_mapping_store（SQLite単一コネクション）へ
                # アクセスしている最中に、呼び出し元がid_mapping_store.close()を呼んで
                # しまう競合が起き得る（実際にテストでセグフォルトを起こして発覚した）。
                # 実行中のワーカー数分だけは短時間で完了を待ち、安全に終了させる。
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)

            if errors:
                if len(errors) > 1:
                    logger.error(
                        "[%s] %d件のレコード作成が失敗しました。最初の1件のみ例外として"
                        "送出しますが、残り%d件も直前のログ（create_page()失敗ログ）を"
                        "参照してください",
                        db_key,
                        len(errors),
                        len(errors) - 1,
                    )
                raise errors[0]
        else:
            client = notion_clients[db_key]
            client_label = "単一トークン"
            for record in records:
                _materialize_one(db_key, record, client, total=total, client_label=client_label)

    return MigrationSummary(created=created, skipped_existing=skipped_existing)


_T = TypeVar("_T")


def _count_by_key(
    items: list[_T], key_fn: Callable[[_T], tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """アイテム列を (db_key, name) キーで件数集計する（未解決リレーション/USER型集計の共通処理）。"""
    counts: dict[tuple[str, str], int] = {}
    for item in items:
        key = key_fn(item)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _format_record_for_display(record: Mapping[str, Any]) -> str:
    """辞書1件を非エンジニアにも読める `キー=値, キー=値` 形式へ整形する（BLOCKER9）。

    空欄（None/空文字）の項目は表示から除外し、可読性を優先する。
    """
    parts = [f"{k}={v}" for k, v in record.items() if v not in (None, "")]
    return ", ".join(parts) if parts else "(全項目空欄)"


# notion_dedupe.match_existing_client()が返すNeedsReviewClient.reasonは、内部処理向けの
# 英語文字列（notion_dedupe.py自体は「純粋な突合ロジック」に徹し、表示文言は持たない設計の
# ため）。obasan-qualityレビューINFO対応: 要レビューレポートは人（金沢さん）が実際に読んで
# Notion上の重複を判断する入口のため、write_dedupe_report_csv()と同様に表示側でのみ
# 日本語へ変換する（notion_dedupe.py側のreason文字列自体は変更しない。テストでの厳密な
# 文字列比較は行われていないことを確認済み）。
_REASON_LABELS = {
    "company name matched but postal code conflicts": "会社名一致・郵便番号不一致",
    "normalized name matched but postal code conflicts": "正規化後の会社名一致・郵便番号不一致",
}


def _display_reason(reason: str) -> str:
    if reason in _REASON_LABELS:
        return _REASON_LABELS[reason]
    if reason.startswith("normalized name matched ") and reason.endswith(
        " existing records ambiguously"
    ):
        count = reason.removeprefix("normalized name matched ").removesuffix(
            " existing records ambiguously"
        )
        return f"正規化後の会社名が既存{count}件と曖昧に一致"
    return reason


def print_summary(plan: MigrationPlan, summary: MigrationSummary, *, dry_run: bool) -> None:
    """作成件数・未解決サマリーを先頭に、名寄せの生データ等の詳細情報は末尾に表示する。

    実データでリレーションキー列名の推測が外れ、未解決率が異常に高いまま気づかれない
    リスク（BLOCKER4/5）に対応するため、警告・集計サマリーを詳細情報より優先して表示する
    （WARN6: 詳細より先にサマリーが目立つように）。
    """
    unresolved_counts = _count_by_key(plan.unresolved, lambda u: (u.db_key, u.relation_name))
    user_unresolved_counts = _count_by_key(
        plan.unresolved_user_properties, lambda u: (u.db_key, u.property_name)
    )

    # === 未解決率がしきい値を超えるものがあれば、最も目立つ位置に警告を出す ===
    high_rate_warnings = []
    for key, unresolved_count in unresolved_counts.items():
        attempts = plan.relation_attempts.get(key, 0)
        if attempts == 0:
            continue
        rate = unresolved_count / attempts
        if rate > _UNRESOLVED_RATE_WARNING_THRESHOLD:
            db_key, relation_name = key
            high_rate_warnings.append(
                f"  [{db_key}] {relation_name}: {unresolved_count}/{attempts}件が未解決"
                f"（{rate:.0%}）。リレーションキー列名の推測が実データとズレている"
                "可能性があります。要確認。"
            )
    if high_rate_warnings:
        print(f"\n{'!' * 60}")
        print("!!! 警告: リレーション未解決率が高いDB/項目があります !!!")
        for line in high_rate_warnings:
            print(line)
        print("!" * 60)

    verb = "作成予定" if dry_run else "作成"
    print(f"\n=== 移行結果サマリー（{'dry-run' if dry_run else '本番実行'}） ===")
    for db_key in _MATERIALIZATION_ORDER:
        schema = get_schema(db_key)
        line = f"  [{schema.display_name}] {verb}: {summary.created[db_key]}件"
        if not dry_run:
            line += f" / 既存スキップ: {summary.skipped_existing[db_key]}件"
        print(line)

    if plan.skipped_transform_errors:
        print(f"\n変換エラーでスキップしたレコード: {len(plan.skipped_transform_errors)}件")
        for msg in plan.skipped_transform_errors:
            print(f"  - {msg}")

    print(f"\n=== リレーション未解決サマリー: {len(plan.unresolved)}件 ===")
    if not unresolved_counts:
        print("  未解決なし")
    for (db_key, relation_name), count in unresolved_counts.items():
        attempts = plan.relation_attempts.get((db_key, relation_name))
        rate_display = f"（未解決率{count / attempts:.0%}）" if attempts else ""
        print(f"  [{db_key}] {relation_name}: {count}件{rate_display}")

    print(f"\n=== USER型未設定サマリー（担当営業／担当メンバー）: {len(plan.unresolved_user_properties)}件 ===")
    if not user_unresolved_counts:
        print("  未設定なし")
    for (db_key, property_name), count in user_unresolved_counts.items():
        print(f"  [{db_key}] {property_name}: {count}件（氏名→NotionユーザーID対応表が無いため未設定。手動割当が必要）")

    # obasan-qualityレビューWARN対応: 件数だけは他の集計サマリー（未解決・USER型未設定）と
    # 同様に先頭付近へ出す（print_summary()自体のdocstringが明言する「集計サマリーを詳細
    # 情報より優先して表示する」原則に合わせる。個別明細は末尾の詳細セクションに残す）。
    print(f"\n=== 取引先マスター要レビューサマリー: {len(plan.needs_review_clients)}件 ===")
    if not plan.needs_review_clients:
        print("  要レビューなし")
    else:
        print("  （会社名は一致したが郵便番号の食い違い等で自動確定できず、安全側で新規作成した"
              "ケース。金沢さん方針によりスキップはせず作成済み。全件はCSVレポートを参照）")

    print(f"\n=== 名寄せ結果（{len(plan.dedupe_report)}件を統合） ===")
    for entry in plan.dedupe_report:
        print(f"  [{entry.db_key}] key={entry.dedupe_key} ({len(entry.sources)}件を統合)")
        for source in entry.sources:
            print(f"    - {_format_record_for_display(source)}")
        print(f"    => {_format_record_for_display(entry.merged)}")

    print("\n=== リレーション未解決 詳細一覧（全件はCSVレポートを参照） ===")
    for item in plan.unresolved:
        print(
            f"  [{item.db_key}] kintone_id={item.kintone_id} "
            f"{item.relation_name}={item.raw_value!r} が解決できませんでした"
        )

    print("\n=== 取引先マスター要レビュー 詳細一覧（全件はCSVレポートを参照） ===")
    for entry in plan.needs_review_clients:
        print(
            f"  [{entry.source}] external_id={entry.external_id} name={entry.name!r} "
            f"reason={_display_reason(entry.reason)} candidate_page_id={entry.candidate_page_id}"
        )


def write_dedupe_report_csv(dedupe_report: list[DedupeReportEntry], path: Path) -> None:
    """名寄せ結果の目視検証レポートをCSVへ書き出す（04節末尾「⑤名寄せ結果の目視検証」）。

    セルは生JSON文字列ではなく、非エンジニアにも読める `キー=値` 形式で書き出す（BLOCKER9）。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["db_key", "dedupe_key", "source_count", "sources", "merged"])
        for entry in dedupe_report:
            sources_display = " / ".join(_format_record_for_display(s) for s in entry.sources)
            writer.writerow(
                [
                    entry.db_key,
                    entry.dedupe_key,
                    len(entry.sources),
                    sources_display,
                    _format_record_for_display(entry.merged),
                ]
            )


def write_unresolved_report_csv(unresolved: list[UnresolvedRelation], path: Path) -> None:
    """未解決リレーションの全件をCSVへ書き出す（BLOCKER4/5）。

    print_summary()のコンソール出力は集計サマリーのみに絞っているため、実データ検証時に
    全件を確認できる経路としてCSV出力を用意する。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["db_key", "kintone_id", "relation_name", "raw_value"])
        for item in unresolved:
            writer.writerow([item.db_key, item.kintone_id, item.relation_name, item.raw_value])


def write_unresolved_user_report_csv(entries: list[UnresolvedUserProperty], path: Path) -> None:
    """USER型プロパティ（担当営業／担当メンバー）が未設定のまま作成されたレコードをCSVへ
    書き出す（BLOCKER4/5）。Notion側での手動割当作業のチェックリストとして使う想定。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["db_key", "kintone_id", "property_name", "raw_value"])
        for entry in entries:
            writer.writerow([entry.db_key, entry.kintone_id, entry.property_name, entry.raw_value])


def write_needs_review_clients_report_csv(entries: list[NeedsReviewClient], path: Path) -> None:
    """取引先マスターの要レビュー一覧（会社名一致・郵便番号不一致等）をCSVへ書き出す。

    金沢さん方針（2026-08-10、データ欠損より重複の方がマシ）により、該当ケースは
    スキップせず新規作成した上でこのレポートに記録する。全件はここで確認し、
    重複が疑われるものは後から人の目でNotion上を突合・統合する運用を想定している。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "external_id", "name", "reason", "candidate_page_id"])
        for entry in entries:
            writer.writerow(
                [
                    entry.source,
                    entry.external_id,
                    entry.name,
                    _display_reason(entry.reason),
                    entry.candidate_page_id,
                ]
            )
