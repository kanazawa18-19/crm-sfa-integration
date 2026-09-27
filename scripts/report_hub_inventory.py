#!/usr/bin/env python3
"""完成した未連携調査を本人DMへ送る。既定はプレビューのみ。"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from src.db_schema.registry import ALL_SCHEMAS


def render_report(reports, now):
    expected={s.key for s in ALL_SCHEMAS}
    if len(reports)!=len(expected) or {r['db'] for r in reports}!=expected:
        raise ValueError('6DBの全件完了結果が必要です')
    labels={'client_master':'取引先マスター','chain':'チェーン','contact':'連絡先','project':'案件管理','product':'サービス・商品','action':'アクション履歴'}
    lines=['【CRM未連携レコード：週次点検】','既存レコードも含む対応表の未登録件数です。この一覧から一括で自動作成は行いません。']
    for report in reports:
        age=(now-datetime.fromisoformat(report['observed_at'])).total_seconds()
        if age < -60 or age > 6*3600: raise ValueError('今回の調査結果ではありません')
        review=sum(n for kind,n in report['sheet_classes'].items() if kind not in ('mapped','mapped_key_without_row_index','mapped_key_at_other_row'))
        linked_by_key=sum(report['sheet_classes'].get(k,0) for k in ('mapped_key_without_row_index','mapped_key_at_other_row'))
        lines.append(f"{labels[report['db']]}：Notion未登録 {report['notion_unmapped']:,}/{report['notion_total']:,}、シート要確認 {review:,}/{report['sheet_nonempty_rows']:,}")
        if linked_by_key:lines.append(f'  同期キーで対応する既存行 {linked_by_key:,}（新規未登録には含めない）')
        recent=sorted(report['notion_unmapped_pages'],key=lambda p:p['created_time'],reverse=True)[:3]
        for page in recent:
            page_id=page['id'].replace('-','')
            if not re.fullmatch('[0-9a-fA-F]{32}',page_id): raise ValueError('ページIDが不正です')
            lines.append('  作成 '+page['created_time'][:10]+' https://www.notion.so/'+page_id)
        row_numbers=[str(r['row']) for r in report['sheet_review_rows'] if r['classification'] not in ('mapped_key_without_row_index','mapped_key_at_other_row')][:10]
        if row_numbers:lines.append('  シート要確認行（先頭10件）：'+', '.join(row_numbers))
    lines.append('全件一覧は scripts/inventory_unmapped_hub_records.py で再取得。走査中の変更による時間差があります。')
    return '\n'.join(lines)


def send(text):
    token=os.environ['HUB_INVENTORY_SLACK_BOT_TOKEN']
    channel=os.environ['HUB_INVENTORY_SLACK_DM_CHANNEL']
    if not re.fullmatch('D[A-Z0-9]+',channel): raise ValueError('本人DMの設定が必要です')
    r=requests.post('https://slack.com/api/chat.postMessage',headers={'Authorization':'Bearer '+token},
                    json={'channel':channel,'text':text,'unfurl_links':False,'unfurl_media':False},timeout=20,allow_redirects=False)
    body=r.json()
    if r.status_code!=200 or not body.get('ok') or body.get('channel')!=channel or not body.get('ts'):
        raise RuntimeError('Slack通知が受理されませんでした')
    print('Slack受理確認済み')


def append_creation_report(text):
    if os.environ.get("HUB_CREATION_ENABLED_SINCE"):
        from src.hub_creation.journal import PostgresCreationJournal
        journal = PostgresCreationJournal()
        counts = journal.pending_counts()
        text += "\n【条件付き新規登録の保留】"
        text += "\n" + (" / ".join(f"{r['target']} {r['state']}: {r['count']}件" for r in counts) or "0件")
        for row in journal.pending_details():
            source = row['sourceKey']
            location = "https://www.notion.so/" + source[7:].replace("-", "") if source.startswith("notion:") else "シート登録キー " + source[6:]
            text += f"\n{row['target']}: {row['reason']} — {location}"
    return text


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',default='migration_output/hub_inventory')
    p.add_argument('--send',action='store_true')
    p.add_argument('--failed',action='store_true')
    args=p.parse_args()
    if args.failed:text='【CRM未連携レコード：調査未完了】今週の全件点検または結果通知を完了できませんでした。未登録0件・正常とは判定していません。GitHub Actionsの「未連携レコード週次点検」を確認してください。'
    else:text=render_report([json.loads((Path(args.directory)/f'{s.key}.json').read_text()) for s in ALL_SCHEMAS],datetime.now(timezone.utc))
    if not args.failed:text=append_creation_report(text)
    if args.send:send(text)
    else:print(text)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('報告失敗: '+type(exc).__name__,file=sys.stderr);sys.exit(1)
