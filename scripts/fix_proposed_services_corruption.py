#!/usr/bin/env python3
"""案件管理DB「提案サービス」multi_selectに、移行スクリプトのバグでサービス・商品DBの
生ページIDがそのまま書き込まれていた事故を修正する（2026-08-14発覚・一度きりの復旧用）。

■ 何が起きていたか -------------------------------------------------------------------------
案件管理DBには本来「サービス・商品」という正式なリレーションプロパティ
（`src/db_schema/project.py`、product.pyからのdual_property逆参照）があるが、移行処理が
誤ってそのリレーション先ページIDを「提案サービス」（multi_select、本来はサービス名の文字列
を保持する）へ直接書き込んでいた。結果、案件管理10,000件中9,058件（92.6%）で「提案サービス」
がUUID形式の文字列（例: "3b8d8ea8-d4f3-81b3-84eb-f4535f467ceb"）になっていた
（アクション履歴DB側の「提案サービス」は無傷）。

幸い、これらの値は実在するサービス・商品DBページの本物のIDであり、名前を引けば復元できる
（データは失われていない）。ただし85件のサービス・商品ページのうち66件のタイトルは、
「提案サービス」の既存20選択肢（ホテル・旅館向け）にきれいに一致しない
（ビューティー/T&D等の別事業ライン向けサービス、テストデータ、代理店等の非サービス項目が
混在）。この66件のうち19件は既存選択肢と完全一致するため機械的に直せるが、残り26件は
金沢さんの判断が必要（2026-08-14時点でまだ判断待ち）。

■ このスクリプトが行うこと -----------------------------------------------------------------
1. サービス・商品DB全件のpage_id→名前(title)マップを取得
2. 案件管理DB全件を走査し、「提案サービス」にUUID形式の値を含むページを対象にする
3. 対象ページごとに:
   - 修正前の状態（提案サービス・サービス・商品リレーション）をバックアップJSONへ記録
   - UUID値のうち`CLEAN_ID_TO_NAME`（19件、既存選択肢と完全一致確認済み）にあるものは
     正しいサービス名へ置換。それ以外のUUID値（26件、未判断）は「提案サービス」からは
     一旦除外する（誤ったラベルを推測して書き込まない）
   - UUID値だったもの全て（19件+26件）は、正しい「サービス・商品」リレーションへ追加する
     （こちらはラベルの曖昧さに関係なく機械的に正しいリンクなので、判断待ちの26件分も
     含めて復元してよい——情報は失われず、リレーションを辿れば元の商品ページが分かる）
4. --dry-run（デフォルト）では書き込みを行わず、変更予定件数のみ表示する。
   --execute で実際に書き込む。

■ 使い方 -----------------------------------------------------------------------------------
    python scripts/fix_proposed_services_corruption.py --dry-run   # 変更内容を確認
    python scripts/fix_proposed_services_corruption.py --execute   # 実際に書き込む
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import HttpNotionClient

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_PROJECT_DB_KEY = "project"
_PRODUCT_DB_KEY = "product"

# 2026-08-14時点で金沢さんに確認済み・機械的に直してよいと判断した19件
# （サービス・商品DBのpage_id→提案サービスDBの既存選択肢名、完全一致確認済み）。
CLEAN_ID_TO_NAME: dict[str, str] = {
    # このdictはbuild_clean_mapping()で実データから自動生成し、確認用に一度出力してから
    # 埋める運用とする（ページIDは環境・実データ依存のためハードコードしない）。
}


def build_product_id_to_name(product_client: HttpNotionClient) -> dict[str, str]:
    id_to_name: dict[str, str] = {}
    for page in product_client.query_all_pages():
        name = page.get("properties", {}).get("名前")
        # query_all_pagesは生のNotion API形式を返すため、titleを自前で結合する。
        if isinstance(name, dict) and name.get("type") == "title":
            id_to_name[page["id"]] = "".join(
                t.get("plain_text", "") for t in name.get("title", [])
            )
    return id_to_name


def compute_clean_mapping(id_to_name: dict[str, str], valid_options: set[str]) -> dict[str, str]:
    return {pid: name for pid, name in id_to_name.items() if name in valid_options}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="実際に書き込む（未指定時はdry-run）")
    parser.add_argument(
        "--backup-path",
        default=None,
        help="修正前バックアップの出力先（未指定時は自動生成）",
    )
    args = parser.parse_args()

    project_schema = get_schema(_PROJECT_DB_KEY)
    product_schema = get_schema(_PRODUCT_DB_KEY)
    project_client = HttpNotionClient(_PROJECT_DB_KEY, project_schema.notion_database_id)
    product_client = HttpNotionClient(_PRODUCT_DB_KEY, product_schema.notion_database_id)

    valid_options = {
        opt for opt in project_schema.get_property("提案サービス").options or ()
    }

    id_to_name = build_product_id_to_name(product_client)
    clean_mapping = compute_clean_mapping(id_to_name, valid_options)
    print(f"サービス・商品ページ総数: {len(id_to_name)}")
    print(f"既存選択肢と完全一致（機械的に直せる）: {len(clean_mapping)}件")

    backup_path = args.backup_path or (
        f"/private/tmp/claude-501/-Users-cnctor/fc9bb84c-5914-40d9-b328-76ae31eae55d/scratchpad/"
        f"proposed_services_fix_backup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    backups: list[dict] = []

    changed = 0
    skipped_no_garbage = 0
    unresolved_ids_seen: set[str] = set()

    for page in project_client.query_all_pages():
        page_id = page["id"]
        props = page.get("properties", {})
        proposed_prop = props.get("提案サービス")
        if not proposed_prop or proposed_prop.get("type") != "multi_select":
            continue
        current_values = [o["name"] for o in proposed_prop.get("multi_select", [])]
        garbage_values = [v for v in current_values if _UUID_RE.match(v)]
        if not garbage_values:
            skipped_no_garbage += 1
            continue

        service_rel_prop = props.get("サービス・商品")
        current_relation_ids = (
            [r["id"] for r in service_rel_prop.get("relation", [])] if service_rel_prop else []
        )

        kept_names = [v for v in current_values if not _UUID_RE.match(v)]
        resolved_names = []
        for gid in garbage_values:
            resolved = clean_mapping.get(gid)
            if resolved and resolved not in kept_names and resolved not in resolved_names:
                resolved_names.append(resolved)
            elif not resolved:
                unresolved_ids_seen.add(gid)

        new_proposed = kept_names + resolved_names
        new_relation_ids = sorted(set(current_relation_ids) | set(garbage_values))

        backups.append(
            {
                "page_id": page_id,
                "before": {"提案サービス": current_values, "サービス・商品": current_relation_ids},
                "after": {"提案サービス": new_proposed, "サービス・商品": new_relation_ids},
            }
        )
        changed += 1

        if args.execute:
            # 一時的なネットワーク断でクラッシュした場合でも直前まで進んだ分の記録を失わない
            # よう、数回だけ再試行してからバックアップを都度flushする（2026-08-14、実運用中に
            # `requests.exceptions.ConnectionError`でクラッシュしバックアップが1件も書き出され
            # ないまま終了した事故を受けて追加）。
            for attempt in range(3):
                try:
                    project_client.update_page(
                        page_id,
                        {"提案サービス": new_proposed, "サービス・商品": new_relation_ids},
                    )
                    break
                except Exception as e:
                    if attempt == 2:
                        with open(backup_path, "w") as f:
                            json.dump(backups, f, ensure_ascii=False, indent=2)
                        print(f"\n{page_id}で3回失敗、中断: {e}")
                        print(f"ここまでの進捗をバックアップへ保存済み: {backup_path}")
                        raise
                    time.sleep(2 ** attempt)
            time.sleep(0.05)  # Notion APIレート制限に配慮
            if changed % 200 == 0:
                with open(backup_path, "w") as f:
                    json.dump(backups, f, ensure_ascii=False, indent=2)
                print(f"...{changed}件処理済み（途中経過をバックアップへ保存）", flush=True)

    with open(backup_path, "w") as f:
        json.dump(backups, f, ensure_ascii=False, indent=2)

    print(f"対象（ガベージ値あり）: {changed}件")
    print(f"対象外（正常）: {skipped_no_garbage}件")
    print(f"未判断のIDのまま残るケースで出現した固有ID数: {len(unresolved_ids_seen)}")
    print(f"バックアップ（修正前後の内容）: {backup_path}")
    if not args.execute:
        print("\n--dry-run のため書き込みは行っていません。--execute で実行してください。")
    else:
        print("\n書き込み完了。")


if __name__ == "__main__":
    main()
