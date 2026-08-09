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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypeVar

from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.base import Tool
from src.db_schema.chain import CHAIN_SCHEMA
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.product import PRODUCT_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA
from src.db_schema.registry import get_schema
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
from src.migration.project_mapping import transform_kintone_project
from src.sync_engine.id_mapping import IdMapping, IdMappingStore

logger = logging.getLogger(__name__)

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
class MigrationPlan:
    prepared: dict[str, list[PreparedRecord]]
    unresolved: list[UnresolvedRelation] = field(default_factory=list)
    unresolved_user_properties: list[UnresolvedUserProperty] = field(default_factory=list)
    dedupe_report: list[DedupeReportEntry] = field(default_factory=list)
    skipped_transform_errors: list[str] = field(default_factory=list)
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
) -> MigrationPlan:
    """CSV行から、Notion作成前の全レコード（リレーション解決済み）と各種レポートを組み立てる。

    I/O（Notion API・IDマッピングストア）を一切行わない純粋関数。依存順序が
    ①取引先マスター→②チェーン→③連絡先→④案件管理→⑤サービス・商品→⑥アクション管理
    であるため、この順で処理する。
    """
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
    relation_attempts: dict[tuple[str, str], int] = {}
    ids = TitleIdGenerator()

    def _note_attempt(db_key: str, relation_name: str) -> None:
        """リレーション解決を実際に試みた回数を記録する（未解決率算出の分母、BLOCKER4/5）。"""
        key = (db_key, relation_name)
        relation_attempts[key] = relation_attempts.get(key, 0) + 1

    # === ① 取引先マスター ===================================================
    client_by_name: dict[str, PreparedRecord] = {}
    for row in client_master_rows:
        props = transform_client_master(row)
        # BLOCKER: 以前はここで props[title_property.name]（="取引先名"）を ids.next(...)
        # の連番IDで上書きしており、transform_client_master()が既にセットした実際の会社名が
        # 失われ、Notion上のタイトルが全件"CLI-001"のような連番IDになってしまうバグが
        # あった（実データ検証で発覚）。取引先名は既にtransform_client_master()の戻り値に
        # 含まれているため、ここでの追加代入は不要かつ有害だった。
        record = PreparedRecord("client_master", props["kintone_ID"] or None, props)
        prepared["client_master"].append(record)
        name = props["取引先名"]
        if name:
            # 同名取引先が複数行ある場合、最初に出現したレコードを以降のリレーション解決の
            # 正とする（Q-08の名寄せ対象はあくまで連絡先。取引先自体の名寄せは対象外のため
            # 単純な先勝ちルールに留める）。同名重複はデータ不整合の可能性があるため
            # 気づけるよう警告ログを残す（WARN10）。
            if name in client_by_name:
                logger.warning(
                    "duplicate 取引先名 detected: %r (kintone_id=%s). "
                    "only the first occurrence (kintone_id=%s) is used for relation resolution",
                    name,
                    record.kintone_id,
                    client_by_name[name].kintone_id,
                )
            else:
                client_by_name[name] = record

    # === ② チェーン =========================================================
    chain_by_name: dict[str, PreparedRecord] = {}
    for row in client_master_rows:
        chain_name = extract_chain_name(row)
        if chain_name is None or chain_name in chain_by_name:
            continue
        chain_props: dict[str, Any] = {
            CHAIN_SCHEMA.title_property.name: ids.next("chain"),
            "チェーン名": chain_name,
            # kintone取引先マスタに「グループ名」に相当する個別項目が無いため、
            # チェーン名をそのまま流用する。
            "グループ名": chain_name,
        }
        chain_record = PreparedRecord("chain", f"chain:{chain_name}", chain_props)
        prepared["chain"].append(chain_record)
        chain_by_name[chain_name] = chain_record

    for row, client_record in zip(client_master_rows, prepared["client_master"]):
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
            "初期費用（イニシャル）": transformed["初期費用（イニシャル）"],
            "月額費用（ランニング）": transformed["月額費用（ランニング）"],
            "契約日": transformed["契約日"],
            "予想契約日": transformed["予想契約日"],
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

        action_props = {
            ACTION_SCHEMA.title_property.name: ids.next("action"),
            "アクション種別": transformed["アクション種別"],
            "アクション日": row.get("アクション日") or None,
            "取引先マスター": [client_record] if client_record else [],
            "案件管理": [project_record] if project_record else [],
            "先方担当者": [contact_record] if contact_record else [],
            "履歴メモ": transformed["履歴メモ"],
            "kintone_Act_ID": transformed["kintone_Act_ID"],
        }
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

    return MigrationPlan(
        prepared=prepared,
        unresolved=unresolved,
        unresolved_user_properties=unresolved_user_properties,
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
) -> MigrationSummary:
    """計画済みレコードを実際にNotionへ作成し、IDマッピングストアへ登録する。

    dry_run=True の場合、Notion API・IDマッピングストアへは一切アクセスしない
    （引数に何を渡していても呼び出さない）。
    """
    created = {key: 0 for key in _MATERIALIZATION_ORDER}
    skipped_existing = {key: 0 for key in _MATERIALIZATION_ORDER}

    for db_key in _MATERIALIZATION_ORDER:
        for record in plan.prepared[db_key]:
            if dry_run:
                record.notion_key = business_id(record)
                created[db_key] += 1
                continue

            if id_mapping_store is None or notion_clients is None:
                raise ValueError("dry_run=False の場合、id_mapping_store/notion_clients は必須です")

            existing = (
                id_mapping_store.find_by_external_id(Tool.KINTONE, record.kintone_id)
                if record.kintone_id
                else None
            )
            if existing is not None:
                # 一度Notionへ移行済みのレコードは、以後Notionを正とする運用方針
                # （05_同期・競合制御「Notion優先」原則）に合わせ、再実行時は上書きせず
                # スキップする（このバッチが手動修正結果を巻き戻さないようにするため）。
                record.notion_key = existing.notion_key
                skipped_existing[db_key] += 1
                continue

            client = notion_clients[db_key]
            page_id = client.create_page(resolved_properties(record))
            record.notion_key = page_id
            created[db_key] += 1
            id_mapping_store.upsert(
                IdMapping(notion_key=page_id, db_key=db_key, kintone_id=record.kintone_id)
            )

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
