"""前回計画の未完了だけを、実在・対応表・本文を再確認して補完する。"""
import argparse, hashlib, json, logging, os, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv
load_dotenv(REPO / 'config/.env')
logging.disable(logging.CRITICAL)
from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.production_wiring import build_notion_clients_by_db
from src.sync_engine.clients.zoho_client import HttpZohoClient
from src.sync_engine.notion_id_mapping import NotionIdMappingStore
from src.sync_engine.zoho_field_mapping import resolve_zoho_field_label
from src.sync_engine.sync_notes import gap_notes, MARKER, parse_notes, merge_notes
from src.sync_engine.record_sync_lock import acquire_record_sync_lock
from src.record_merge.notion_gateway import NotionMergeGateway
from src.record_merge.domain import stable_blocks
from src.record_merge.aliases import require_record_available

ROOT = REPO / 'migration_output/remaining-20260928'
RESULT = ROOT / 'limited-notes-result.json'
def h(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def content(b): return ''.join(x.get('plain_text',x.get('text',{}).get('content','')) for x in b.get('callout',{}).get('rich_text',[]))
def managed(b): return b.get('type')=='callout' and content(b).startswith(MARKER+'\n')
def note_state(blocks):
    result={}
    for b in reversed(blocks):
        if managed(b): result.update(parse_notes(content(b)))
    return result

def notes_complete(blocks, expected):
    # 現在の元値に未解決項目がなければ、空の本文をそのまま完了とする。
    if not blocks:
        return not expected
    return len(blocks)==1 and managed(blocks[0]) and note_state(blocks)==expected

def main():
    args=argparse.ArgumentParser();args.add_argument('--apply',action='store_true');args.add_argument('--limit',type=int,default=1); opts=args.parse_args()
    previous=json.loads(RESULT.read_text()); done={r['notion_key'] for r in previous if r['status'] in {'verified','already_complete'}}
    plans={r['notion_key']:r for r in json.loads((ROOT/'limited-notes-plan.json').read_text())}
    sources={r['notion_key']:r for r in json.loads((ROOT/'relation-sources-live-verified.json').read_text())}
    todo=[p for k,p in plans.items() if k not in done and p['status']=='planned'][:opts.limit]
    clients=build_notion_clients_by_db(max_rate_limit_retries=2)
    store=NotionIdMappingStore(api_key=os.environ['NOTION_API_KEY'],max_rate_limit_retries=2)
    gateway=NotionMergeGateway(clients,store)
    zoho=HttpZohoClient(accounts_base_url=os.environ['ZOHO_ACCOUNTS_BASE_URL'],api_base_url=os.environ['ZOHO_API_BASE_URL'],max_retries=1)
    successes=0
    for index,p in enumerate(todo):
        db=p['db_key']; key=p['notion_key']; source=sources[key]
        try:
            with acquire_record_sync_lock(None,db,key):
                require_record_available(db,key)
                mapping=store.get(key)
                if mapping is None or mapping.db_key!=db or mapping.notion_key.replace('-','')!=key.replace('-','') or str(mapping.zoho_id)!=str(source['zoho_id']): raise ValueError('対応表不一致')
                module=get_schema(db).zoho_api_module
                native=zoho.get_record(module,source['zoho_id'])
                if not isinstance(native,dict) or not native: raise ValueError('元レコード未確認')
                values={(resolve_zoho_field_label(module,k) or '').removeprefix('【Notion】'):v for k,v in native.items()}
                if db in ('chain','contact') and 'Owner' in native:
                    owner=native['Owner']; name=owner.get('name') if isinstance(owner,dict) else owner
                    values['担当' if db=='chain' else '担当メンバー']='Owner（送信元の担当者）: '+str(name) if name else ''
                notes=gap_notes(Tool.ZOHO,db,values)
                page=gateway.page(db,key)
                if page.get('archived') or page.get('in_trash'): raise ValueError('ページ無効')
                blocks=gateway.blocks(db,key); body=[b for b in blocks if not managed(b)]
                # 前回計画が空本文だったページだけ扱う。本文のあるものは推測せず保留。
                if body or p['body_hash_before']!=h([]): raise ValueError('本文再確認が必要')
                old=note_state(blocks); expected=merge_notes(old,notes)
                complete=notes_complete(blocks,expected)
                if not opts.apply:
                    print(json.dumps({'index':index+1,'db':db,'notes':len(expected),'already_complete':complete,'mode':'dry-run'}),flush=True);continue
                if not complete:
                    # 書込直前にも本文・ページの変更を検出する。
                    latest=gateway.page(db,key)
                    if latest.get('last_edited_time')!=page.get('last_edited_time') or stable_blocks(gateway.blocks(db,key))!=stable_blocks(blocks): raise ValueError('直前変更')
                    clients[db].upsert_sync_notes(key,notes)
                after=gateway.blocks(db,key)
                if [b for b in after if not managed(b)]!=body or not notes_complete(after,expected): raise ValueError('読戻し不一致')
                result={'db_key':db,'notion_key':key,'status':'already_complete' if complete else 'verified','notes_count':len(expected),'body_hash_before':p['body_hash_before'],'body_preserved':True}
                previous.append(result)
                temporary=RESULT.with_suffix('.pending.json');temporary.write_text(json.dumps(previous,ensure_ascii=False,indent=2));temporary.replace(RESULT)
                successes+=1
                print(json.dumps({'verified_this_run':successes,'remaining':len(plans)-len(done)-successes}),flush=True)
        except Exception as exc:
            print(json.dumps({'stopped_at':index+1,'error_type':type(exc).__name__, 'reason': str(exc) if str(exc) in {'対応表不一致','元レコード未確認','ページ無効','本文再確認が必要','直前変更','読戻し不一致'} else '詳細非表示'}),flush=True)
            return 1
    return 0
if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc: print(json.dumps({'setup_error':type(exc).__name__}));sys.exit(1)
