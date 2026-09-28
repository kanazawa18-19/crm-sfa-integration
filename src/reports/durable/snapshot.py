"""日報・週報の集計に使う項目だけを、元のNotion形式で保存する。"""

# batch.py の集計項目追加時は、この一覧と test_snapshot.py も更新する。
# 全期間の接触回数を集計するためページ自体は間引かない。
REPORT_PROPERTIES = {
    'project': frozenset({
        '案件名', '営業ステータス', '確度', '初期費用', '月額費用',
        '担当メンバー', '次回アクション日', '提案サービス', '作成日時',
        '契約日 / 予想契約日',
    }),
    'action': frozenset({
        '商談回数・電話回数・メール回数（何回目）', 'アクション日', '案件名', '担当営業',
    }),
}


def report_snapshot(db_key, page):
    """集計値・担当者の名前解決を変えず、本文・画像URL等の不要な保存を避ける。"""
    names = REPORT_PROPERTIES[db_key]
    return {'id': page['id'], 'created_time': page.get('created_time'),
            'properties': {name: value for name, value in page['properties'].items() if name in names}}
