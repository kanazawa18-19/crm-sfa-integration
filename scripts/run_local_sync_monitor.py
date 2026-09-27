#!/usr/bin/env python3
"""主機の定期点検。既存キーチェーン・ローカル設定を使い、クラウドへ追加配布しない。

毎時の受信監視と週次の全件照合を独立ジョブで実行する。主機停止中は実行できない。
状態はSlack受理後にだけ確定し、失敗はログと日次・週次報告の不達で分かるようにする。
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from scripts.report_hub_inventory import render_report, send, append_creation_report
from src.db_schema.registry import ALL_SCHEMAS

ROOT=Path(__file__).resolve().parents[1]
STATE=Path.home()/'Library/Application Support/crm-sfa-integration'
CHANNEL='D0BNE3Y0P9Q'


def keychain(name):
    result=subprocess.run(['/usr/bin/security','find-generic-password','-s',name,'-w'],capture_output=True,text=True,timeout=20)
    if result.returncode or not result.stdout.strip():raise RuntimeError('認証情報を取得できません')
    return result.stdout.strip()


def save(path, value):
    temp=path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.chmod(temp,0o600);json.dump(value,stream,ensure_ascii=False)
    temp.replace(path)


def health_plan(previous):
    report=None
    try:
        result=requests.get('https://crm-sfa-integration.vercel.app/api/diagnostics/webhook-health',
            headers={'Authorization':'Bearer '+keychain('crm-sfa-webhook-health-token')},timeout=110,allow_redirects=False)
        if result.status_code==200:report=result.json()
    except Exception:pass
    code="""const h=require('./gas/webhook-health/domain');let s='';process.stdin.on('data',c=>s+=c);
process.stdin.on('end',()=>{const x=JSON.parse(s);let checks;
try{checks=h.validate(x.report,x.now).checks.concat([{key:'monitor',status:'ok',reason:'診断APIへ到達・応答確認'}]);}
catch(_){checks=[{key:'monitor',status:'unknown',reason:'診断APIの到達・認証・応答を確認できません'}];}
console.log(JSON.stringify(h.plan(checks,x.previous,x.now)));});"""
    now=int(time.time()*1000)
    process=subprocess.run([str(Path.home()/'.nodebrew/current/bin/node'),'-e',code],cwd=ROOT,
        input=json.dumps({'report':report,'previous':previous,'now':now}),capture_output=True,text=True,timeout=20)
    if process.returncode:raise RuntimeError('監視判定に失敗しました')
    return json.loads(process.stdout)


def health_messages(plan):
    messages=[]
    if plan['events']:
        labels={'recovered':'復旧','critical':'異常','unknown':'監視不能','warning':'要確認'}
        messages.append('【CRM連携監視：状態変化】\n'+'\n'.join(labels[e['level']]+'｜'+e['text'] for e in plan['events']))
    if plan['daily']:
        lines=plan['lines']
        if len(lines)==1:lines=lines+['Notion・Zoho・kintone・シート：今回の状態は確認不能（前回状態を保持）']
        messages.append('【CRM連携監視：日次報告】\n'+'\n'.join(lines)+'\n主機で毎時監視。電源断・スリープ・ログイン前は停止します。受信は同期成功の保証ではなく、一部DBだけの停止も対象外です。日次報告が来ない場合は監視自体を確認してください。')
    return messages


def inventory_report(directory):
    return append_creation_report(render_report([json.loads((directory/f'{s.key}.json').read_text()) for s in ALL_SCHEMAS],datetime.now(timezone.utc)))


def notify_failure(job):
    """点検自体の故障も同じジョブでは24時間に1回まで。受理後だけ記録する。"""
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=STATE/(job+'.failure.json')
    with (STATE/(job+'.failure.lock')).open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        try:last=float(json.loads(path.read_text())['notified_at'])
        except (OSError,ValueError,KeyError,TypeError):last=0
        now=time.time()
        if 0<=now-last<86400:return
        os.environ['HUB_INVENTORY_SLACK_BOT_TOKEN']=keychain('crm-sfa-slack-bot-token')
        os.environ['HUB_INVENTORY_SLACK_DM_CHANNEL']=CHANNEL
        label='毎時監視' if job=='health' else '週次全件点検'
        send('【CRM定期点検：実行失敗】'+label+'または通知が完了しませんでした。正常とは判定していません。主機のcrm-sfa-integration定期点検ログを確認してください。')
        save(path,{'notified_at':now})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job',choices=['health','inventory'])
    parser.add_argument('--preview',action='store_true')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--inventory-directory',default=str(ROOT/'migration_output/hub_inventory'))
    from dotenv import load_dotenv
    load_dotenv(ROOT/"config/.env", override=False)
    args=parser.parse_args();STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    state_path=STATE/(args.job+'.json')
    if args.status:
        print(state_path.read_text() if state_path.exists() else '{}');return
    with (STATE/(args.job+'.lock')).open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print('同じ点検を実行中のため保留');return
        previous=json.loads(state_path.read_text()) if state_path.exists() else {}
        started=time.monotonic();next_state={}
        if args.job=='health':
            plan=health_plan(previous.get('notification_state'));messages=health_messages(plan)
            next_state['notification_state']=plan['next']
        else:
            if args.preview:
                directory=Path(previous.get('directory',args.inventory_directory))
                try:message=inventory_report(directory)
                except (OSError,ValueError,KeyError,TypeError):raise RuntimeError('プレビューに必要な6DBの今回結果がありません')
            else:
                directory=ROOT/'migration_output/weekly_inventory'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                result=subprocess.run([sys.executable,str(ROOT/'scripts/inventory_unmapped_hub_records.py'),
                    '--mapping-token-env','NOTION_API_KEY','--quiet','--output-dir',str(directory)],cwd=ROOT,timeout=9900,capture_output=True,text=True)
                if result.returncode:raise RuntimeError('全件点検を完了できませんでした')
                message=inventory_report(directory)
            messages=[message];next_state['directory']=str(directory)
        if args.preview:print('\n\n'.join(messages));return
        os.environ['HUB_INVENTORY_SLACK_BOT_TOKEN']=keychain('crm-sfa-slack-bot-token')
        os.environ['HUB_INVENTORY_SLACK_DM_CHANNEL']=CHANNEL
        for message in messages:send(message)
        next_state.update(last_success=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-started,1),messages=len(messages))
        save(state_path,next_state)
        print(json.dumps({k:v for k,v in next_state.items() if k not in ('notification_state','directory')}),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # 例外本文には認証値を含む可能性がある。送信失敗時も状態を進めない。
        print('定期点検失敗: '+type(exc).__name__,file=sys.stderr)
        try:
            if '--preview' in sys.argv or '--status' in sys.argv:raise RuntimeError('表示専用では通知しない')
            job=next((arg for arg in sys.argv[1:] if arg in ('health','inventory')),None)
            if job:notify_failure(job)
        except Exception:pass
        sys.exit(1)
