#!/usr/bin/env python3
"""主機だけに2本のlaunchdを設置。既定は設定プレビュー。秘密値はplistに書かない。"""
from pathlib import Path
import argparse
import os
import plistlib
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');args=p.parse_args()
    agents=Path.home()/'Library/LaunchAgents';logs=Path.home()/'Library/Logs/crm-sfa-integration'
    for job in ('health','inventory'):
        label='jp.cnctor.crm-sfa.'+job
        config={'Label':label,'ProgramArguments':[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/run_local_sync_monitor.py'),job],
                'WorkingDirectory':str(ROOT),'StandardOutPath':str(logs/(job+'.log')),'StandardErrorPath':str(logs/(job+'.error.log')),
                'EnvironmentVariables':{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'}}
        if job=='health':config['StartInterval']=3600
        else:config['StartCalendarInterval']={'Weekday':1,'Hour':6,'Minute':17}
        target=agents/(label+'.plist')
        print(plistlib.dumps(config).decode())
        if not args.apply:continue
        agents.mkdir(parents=True,exist_ok=True);logs.mkdir(parents=True,exist_ok=True)
        domain=f'gui/{os.getuid()}'
        if target.exists() and plistlib.loads(target.read_bytes())!=config:raise RuntimeError('既存設定と異なるため上書きしません')
        target.write_bytes(plistlib.dumps(config))
        exists=subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode==0
        if not exists:subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
        print('登録済み: '+label)

if __name__=='__main__':main()
