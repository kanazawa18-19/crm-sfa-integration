"""外部原本の実読と、Drive・各CRMへの追記。公開権限は変更しない。"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit, urlencode

from src.attachment_sync import http
from src.attachment_sync.domain import (CATEGORIES, MAX_BYTES, Held, Source, drive_id,
    filename, notion_download_url)
from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.document_generation.google_auth import get_google_access_token
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.clients._http import raise_for_error, ApiError
from src.sync_engine.production_wiring import get_production_wiring, build_zoho_client


class Gateway:
    def __init__(self):
        self.config = json.loads((Path(__file__).resolve().parents[2] / 'config/attachment_sync.json').read_text())
        self.folder = os.environ.get('ATTACHMENT_DRIVE_FOLDER_ID', '')
        self.wiring = get_production_wiring()
        self.zoho = build_zoho_client()
        self.mail_tokens = {}

    def notion(self, db):
        # 添付は専用経路だけが変更し、通常の項目同期には混ぜない。
        return HttpNotionClient(db, get_schema(db).notion_database_id, timeout=15,
                                max_retries=0, max_rate_limit_retries=0)

    def page(self, db, record):
        http.check_budget()
        from src.sync_operations.exclusions import is_excluded
        if is_excluded(Tool.NOTION,db,record):raise Held('同期除外の原本です')
        from src.record_merge.aliases import resolve_alias, require_record_available
        if resolve_alias(db, Tool.NOTION, record):
            raise Held('統合済みの原本です')
        require_record_available(db, record)
        raw = self.notion(db).get_raw_page(record)
        if not raw or raw.get('archived') or raw.get('in_trash'):
            raise Held('原本が削除・保管されています')
        expected = get_schema(db).notion_database_id.replace('-', '')
        if (raw.get('parent', {}).get('database_id') or '').replace('-', '') != expected:
            raise Held('原本のDBが一致しません')
        return raw

    def mapping(self, db, record):
        self.page(db, record)
        mapping = self.wiring.id_mapping_store.get(record)
        if not mapping or mapping.db_key != db:
            raise Held('同期先のID対応がありません')
        # 外部IDの逆引きまで確認。別レコードに添付を配送しない。
        for tool, value in ((Tool.ZOHO,mapping.zoho_id),(Tool.KINTONE,mapping.kintone_id)):
            if value:
                from src.sync_operations.exclusions import is_excluded
                if is_excluded(tool,db,str(value)):raise Held('同期除外の配送先です')
                reverse = self.wiring.id_mapping_store.find_by_external_id(tool, str(value), db_key=db)
                if not reverse or reverse.notion_key != record:
                    raise Held('同期先の逆引きが一致しません')
        return mapping

    def google(self):
        return {'Authorization': 'Bearer ' + get_google_access_token()}

    def zoho_url(self, db, record):
        if not self.zoho:
            raise Held('Zoho接続がありません')
        base = self.zoho._api_base_url.rsplit('/crm/',1)[0] + '/crm/v8'
        return base + '/' + quote(get_schema(db).zoho_api_module, safe='') + '/' + quote(str(record),safe='')

    def kintone(self, db, path, method='GET', **kwargs):
        suffix = db.upper()
        headers = {'X-Cybozu-API-Token': os.environ['KINTONE_API_TOKEN_' + suffix]}
        return http.request(method, 'https://' + os.environ['KINTONE_DOMAIN'] + '/k/v1/' + path,
                            headers=headers, **kwargs)

    def krecord(self, db, record):
        return self.kintone(db,'record.json',params={'app':os.environ['KINTONE_APP_ID_'+db.upper()],'id':record}).json()['record']

    def notion_id(self, item):
        if item.get('type') == 'external' and (key := drive_id(item['external']['url'])):
            return 'drive:' + key
        url = (item.get(item.get('type')) or {}).get('url','')
        return hashlib.sha256((urlsplit(url).path + '\0' + item.get('name','')).encode()).hexdigest()

    def discover(self, db, cursor):
        body = {'page_size': 5, 'sorts':[{'timestamp':'created_time','direction':'ascending'}]}
        if cursor.get('watermark'):
            body['filter']={'timestamp':'created_time','created_time':{'on_or_after':cursor['watermark']}}
        if cursor.get('cursor'):
            body['start_cursor']=cursor['cursor']
        data=self.notion(db).query_raw(body)
        pages=data.get('results',[])
        count=cursor.get('count',0)+len(pages)
        if count>=9000:
            watermark=pages[-1]['created_time'] if pages else cursor.get('watermark')
            if not watermark or watermark==cursor.get('watermark'):
                raise Held('同時刻の添付巡回境界を確認してください')
            after={'watermark':watermark,'count':0}
        elif data.get('has_more'):
            if not data.get('next_cursor'):raise Held('巡回位置がありません')
            after={**cursor,'count':count,'cursor':data['next_cursor']}
        elif (data.get('request_status') or {}).get('type')=='incomplete':
            raise Held('添付一覧が途中で切れています')
        else:after={}
        return [p['id'] for p in pages],after

    def sources(self, db, record):
        page=self.page(db,record)
        result=[]
        for category in CATEGORIES[db]:
            prop=page['properties'].get(category,{})
            if prop.get('type')!='files':continue
            for item in prop.get('files',[]):
                result.append(Source('notion',db,record,category,record,self.notion_id(item),item['name'],0,field=category))
        mapping=self.mapping(db,record)
        if mapping.zoho_id:
            url=self.zoho_url(db,mapping.zoho_id)
            z=http.request('GET',url,headers=self.zoho._headers()).json()['data'][0]
            if str(z.get('id'))!=str(mapping.zoho_id):raise Held('Zoho原本が一致しません')
            for category,field in self.config[db]['zoho_fields'].items():
                for item in z.get(field) or []:
                    fid=item.get('attachment_Id') or item.get('id')
                    if not fid:raise Held('Zoho添付の識別子がありません')
                    result.append(Source('zoho',db,record,category,str(mapping.zoho_id),str(fid),item.get('File_Name') or item.get('file_Name') or item.get('file_name') or '添付ファイル',int(item.get('Size') or item.get('size') or 0),field=field))
            for item in self.zattachments(db,str(mapping.zoho_id)):
                # リンク添付はバイナリと別物。自分が作ったリンクは台帳で除外する。
                if not item.get('$file_id'):continue
                result.append(Source('zoho',db,record,'添付ファイル',str(mapping.zoho_id),str(item['id']),item['File_Name'],int(item.get('Size') or 0)))
        if mapping.kintone_id:
            kr=self.krecord(db,str(mapping.kintone_id))
            field=self.config[db]['kintone_field']
            prop=kr.get(field)
            if prop and prop.get('type')=='FILE':
                for item in prop['value']:
                    result.append(Source('kintone',db,record,'添付ファイル',str(mapping.kintone_id),item['fileKey'],item['name'],int(item['size']),field=field))
        return result

    def zattachments(self, db, record):
        result=[]
        for page in range(1,51):
            response=http.request('GET',self.zoho_url(db,record)+'/Attachments',headers=self.zoho._headers(),params={'fields':'id,File_Name,Size,$file_id','page':page,'per_page':200})
            if response.status_code==204:return result
            data=response.json();result.extend(data.get('data',[]))
            if not data.get('info',{}).get('more_records'):return result
        raise Held('Zoho添付一覧の上限です')

    def mail_token(self, owner):
        if owner not in self.mail_tokens:
            from src.gmail_sync.db import find_connection_by_email
            from src.gmail_sync.token_crypto import decrypt_token
            from src.gmail_sync.gmail_client import refresh_access_token
            connection=find_connection_by_email(owner)
            if not connection:raise Held('Gmail接続がありません')
            self.mail_tokens[owner]=refresh_access_token(decrypt_token(connection.refresh_token_enc))
        return {'Authorization':'Bearer '+self.mail_tokens[owner]}

    def mail(self, owner, message, *, require_sent=True):
        raw=http.request('GET','https://gmail.googleapis.com/gmail/v1/users/me/messages/'+quote(message,safe=''),headers=self.mail_token(owner),params={'format':'full'}).json()
        if require_sent and ({'DRAFT','TRASH','SPAM'} & set(raw.get('labelIds',[])) or 'SENT' not in raw.get('labelIds',[])):
            raise Held('送信済みの確定メールではありません')
        return raw

    def mail_parts(self, raw):
        pending=[raw['payload']];result=[];visited=0
        while pending:
            part=pending.pop();visited+=1
            if visited>200:raise Held('メール添付の構造が大きすぎます')
            disposition=next((h['value'] for h in part.get('headers',[]) if h['name'].lower()=='content-disposition'),'').lower()
            if part.get('filename') and not disposition.startswith('inline'):
                result.append(part)
            elif part.get('mimeType')!='message/rfc822':pending.extend(part.get('parts',[]))
        return result

    def mail_sources(self, row):
        raw=self.mail(row['ownerEmail'],row['gmailMessageId'],require_sent=False)
        labels=set(raw.get('labelIds',[]))
        if 'SENT' not in labels or labels & {'DRAFT','TRASH','SPAM'}:return []
        return [Source('gmail','action',row['data']['action_id'],'添付ファイル',row['gmailMessageId'],p.get('partId') or '0',p['filename'],int(p.get('body',{}).get('size') or 0),owner=row['ownerEmail'],field=row['id']) for p in self.mail_parts(raw)]

    def download(self, source):
        mapping=self.mapping(source.db,source.record)
        if source.tool=='notion':
            files=self.page(source.db,source.record)['properties'].get(source.field,{}).get('files',[])
            item=next((v for v in files if self.notion_id(v)==source.file_id),None)
            if not item:raise Held('原本の添付が取り外されています')
            url=item[item['type']]['url']
            if key:=drive_id(url):
                return http.download('https://www.googleapis.com/drive/v3/files/'+quote(key,safe=''),headers=self.google(),params={'alt':'media','supportsAllDrives':'true'})
            if item['type']!='file':raise Held('Drive以外の外部リンクは自動取得しません')
            return http.download(notion_download_url(url))
        if source.tool=='zoho':
            if str(mapping.zoho_id)!=source.external_record:raise Held('Zoho対応先が変わりました')
            url=self.zoho_url(source.db,source.external_record)
            if source.field:
                url+='/actions/download_fields_attachment'
                return http.download(url,headers=self.zoho._headers(),params={'fields_attachment_id':source.file_id})
            return http.download(url+'/Attachments/'+quote(source.file_id,safe=''),headers=self.zoho._headers())
        if source.tool=='kintone':
            if str(mapping.kintone_id)!=source.external_record:raise Held('kintone対応先が変わりました')
            record=self.krecord(source.db,source.external_record)
            if not any(v['fileKey']==source.file_id for v in record.get(source.field,{}).get('value',[])):raise Held('原本の添付が取り外されています')
            return http.download('https://'+os.environ['KINTONE_DOMAIN']+'/k/v1/file.json',headers={'X-Cybozu-API-Token':os.environ['KINTONE_API_TOKEN_'+source.db.upper()]},params={'fileKey':source.file_id})
        # メール受付の訂正・除外後は、以前の計画から添付を運ばない。
        if getattr(self,'mail_guard',None)!=source.key:raise Held('メール受付のロックが必要です')
        raw=self.mail(source.owner,source.external_record)
        part=next((p for p in self.mail_parts(raw) if (p.get('partId') or '0')==source.file_id),None)
        if not part or part['filename']!=source.name:raise Held('メール添付が変わっています')
        body=part.get('body',{})
        if body.get('attachmentId'):
            body=http.request('GET','https://gmail.googleapis.com/gmail/v1/users/me/messages/'+quote(source.external_record,safe='')+'/attachments/'+quote(body['attachmentId'],safe=''),headers=self.mail_token(source.owner)).json()
        encoded=body.get('data','')
        if len(encoded)>MAX_BYTES*4//3+8:raise Held('メール添付が10MB超です')
        return base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4))

    def reserve_drive_id(self):
        if not self.folder:raise Held('添付保存フォルダが未設定です')
        return http.request('GET','https://www.googleapis.com/drive/v3/files/generateIds',headers=self.google(),params={'count':1,'space':'drive','type':'files'}).json()['ids'][0]

    def archive(self, asset, data, check, *, allow_create=False, before_create=lambda:None):
        key=asset['drive_id'];url='https://www.googleapis.com/drive/v3/files/'+key
        # 作成前に永続化したIDで照合する。タイムアウトでも別IDで増やさない。
        r=http.request('GET',url,headers=self.google(),params={'fields':'id,size,md5Checksum,parents,trashed,appProperties','supportsAllDrives':'true'},accepted=(200,404))
        expected=hashlib.md5(data,usedforsecurity=False).hexdigest()
        if r.status_code==404:
            if not allow_create or asset.get('archived'):
                raise Held('保存済みのDrive添付が削除されています')
            meta={'id':key,'name':asset['name'],'parents':[self.folder],'appProperties':{'crmAttachment':asset['id']}}
            from uuid import uuid4
            boundary='crm_'+uuid4().hex
            body=('--'+boundary+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'+json.dumps(meta)+'\r\n--'+boundary+'\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+data+('\r\n--'+boundary+'--\r\n').encode()
            headers={**self.google(),'Content-Type':'multipart/related; boundary='+boundary}
            check()
            http.require_budget(45)
            before_create()
            check()
            http.request('POST','https://www.googleapis.com/upload/drive/v3/files',headers=headers,params={'uploadType':'multipart','supportsAllDrives':'true'},data=body)
            r=http.request('GET',url,headers=self.google(),params={'fields':'id,size,md5Checksum,parents,trashed,appProperties','supportsAllDrives':'true'})
        elif r.status_code!=200:raise http.RemoteError('Drive保存状態を取得できません')
        saved=r.json()
        if saved.get('trashed') or saved.get('md5Checksum')!=expected or int(saved.get('size',-1))!=len(data) or self.folder not in saved.get('parents',[]) or saved.get('appProperties',{}).get('crmAttachment')!=asset['id']:
            raise Held('Drive保存後の内容または保存先が一致しません')
        return 'https://drive.google.com/file/d/'+key+'/view'

    def deliver(self, source, asset, target, receipt, checkpoint, check):
        mapping=self.mapping(source.db,source.record)
        target_record=source.record if target=='notion' else str(getattr(mapping,target+'_id') or '')
        if not target_record:raise Held(target+'のID対応待ちです')
        if receipt and receipt.get('record')!=target_record:
            if receipt.get('state')=='planned':receipt={}
            else:raise Held('添付先のID対応が変更されています')
        if not receipt:
            checkpoint({'state':'planned','record':target_record})
        if receipt.get('state')=='source':return receipt
        original_checkpoint=checkpoint
        checkpoint=lambda value: original_checkpoint({**value,'record':target_record})
        if target=='notion':
            if source.tool=='notion':return {'state':'source','id':source.file_id}
            return self.deliver_notion(source,asset,receipt,checkpoint,check)
        if target=='zoho':
            if not mapping.zoho_id:raise Held('ZohoのID対応待ちです')
            if source.tool=='zoho':return {'state':'source','id':source.file_id}
            return self.deliver_zoho(source,asset,str(mapping.zoho_id),receipt,checkpoint,check)
        if not mapping.kintone_id:raise Held('kintoneのID対応待ちです')
        if source.tool=='kintone':return {'state':'source','id':source.file_id}
        return self.deliver_kintone(source,asset,str(mapping.kintone_id),receipt,checkpoint,check)

    def deliver_notion(self, source, asset, receipt, checkpoint, check):
        # ページ本文へリンクを追記する。添付プロパティ全体の置換による手動編集の消失を避ける。
        client=self.notion(source.db)
        def find():
            cursor=None
            for _ in range(50):
                http.check_budget()
                query=urlencode({'page_size':100,**({'start_cursor':cursor} if cursor else {})})
                response=client._request('GET','/blocks/'+source.record+'/children?'+query)
                raise_for_error(response,ApiError)
                data=response.json()
                for block in data.get('results',[]):
                    if block.get('archived') or block.get('in_trash'):continue
                    for text in block.get('paragraph',{}).get('rich_text',[]):
                        if (text.get('text',{}).get('link') or {}).get('url')==asset['url']:
                            return block['id']
                if not data.get('has_more'):return None
                cursor=data.get('next_cursor')
                if not cursor:raise Held('Notion本文の一覧が不完全です')
            raise Held('Notion本文の確認上限です')
        self.page(source.db,source.record)
        found=find()
        if found:return {'state':'verified','id':found}
        if receipt.get('state') in ('writing','verified'):
            raise Held('Notion添付の結果不明または手動解除。自動復元しません')
        check()
        http.require_budget(45)
        checkpoint({'state':'writing'})
        check()
        response=client._request('PATCH','/blocks/'+source.record+'/children',json_body={'children':[{
            'object':'block','type':'paragraph','paragraph':{'rich_text':[
                {'type':'text','text':{'content':source.category+'：'}},
                {'type':'text','text':{'content':asset['name'],'link':{'url':asset['url']}}}
            ]}}]},idempotent=False)
        raise_for_error(response,ApiError)
        key=find()
        if not key:raise Held('Notion添付リンクの保存を確認できません')
        return {'state':'verified','id':key}

    def deliver_zoho(self, source, asset, record, receipt, checkpoint, check):
        # 関連リストへDriveリンクを追記する。既存のファイルアップロード欄は変更しない。
        title=source.category+'：'+asset['name']+' [CRM:'+asset['id'][:16]+']'
        items=self.zattachments(source.db,record)
        matches=[v for v in items if v.get('File_Name')==title]
        if len(matches)==1 and receipt.get('id')==str(matches[0]['id']):return {'state':'verified','id':str(matches[0]['id'])}
        if matches or receipt.get('state') in ('writing','verified'):raise Held('Zoho添付の結果不明または手動解除。自動で追加しません')
        headers=self.zoho._headers()
        url=self.zoho_url(source.db,record)+'/Attachments'
        check()
        http.require_budget(45)
        checkpoint({'state':'writing'})
        check()
        data=http.request('POST',url,headers=headers,files={'attachmentUrl':(None,asset['url']),'title':(None,title)}).json()['data'][0]
        if data.get('code')!='SUCCESS':raise Held('Zohoが添付を受理していません')
        key=str(data['details']['id']);checkpoint({'state':'writing','id':key})
        if not any(str(v['id'])==key and v.get('File_Name')==title for v in self.zattachments(source.db,record)):raise Held('Zoho添付の保存を確認できません')
        return {'state':'verified','id':key}

    def deliver_kintone(self, source, asset, record, receipt, checkpoint, check):
        field=self.config[source.db]['kintone_field']
        raw=self.krecord(source.db,record);prop=raw.get(field)
        if not prop or prop.get('type')!='FILE':raise Held('kintone添付欄の作成が必要です')
        files=prop['value']
        base=Path(filename(asset['name']))
        name=base.stem+' [CRM-'+asset['id'][:16]+']'+base.suffix
        matches=[v for v in files if v.get('name')==name]
        if len(matches)==1:
            actual=self.kdownload(source.db,matches[0]['fileKey'])
            if hashlib.sha256(actual).hexdigest()!=asset['sha256']:
                raise Held('kintone添付の内容が一致しません')
            return {'state':'verified','id':matches[0]['fileKey']}
        if matches or receipt.get('state') in ('writing','verified'):raise Held('kintone添付の結果不明または手動解除。自動で追加しません')
        if len(files)>=100:raise Held('kintone添付数の上限を確認してください')
        check()
        data=http.download('https://www.googleapis.com/drive/v3/files/'+asset['drive_id'],headers=self.google(),params={'alt':'media','supportsAllDrives':'true'})
        if hashlib.sha256(data).hexdigest()!=asset['sha256']:raise Held('Drive添付が変更されています')
        check()
        upload=self.kintone(source.db,'file.json','POST',files={'file':(name,data,'application/octet-stream')}).json()['fileKey']
        check()
        http.require_budget(45)
        checkpoint({'state':'writing','upload_key':upload})
        check()
        self.kintone(source.db,'record.json','PUT',json={'app':os.environ['KINTONE_APP_ID_'+source.db.upper()],'id':record,'revision':raw['$revision']['value'],'record':{field:{'value':[{'fileKey':v['fileKey']} for v in files]+[{'fileKey':upload}]}}})
        saved=self.krecord(source.db,record)[field]['value']
        matches=[v for v in saved if v.get('name')==name]
        if len(matches)!=1:raise Held('kintone添付の保存を確認できません')
        if hashlib.sha256(self.kdownload(source.db,matches[0]['fileKey'])).hexdigest()!=asset['sha256']:raise Held('kintone保存後の内容が一致しません')
        return {'state':'verified','id':matches[0]['fileKey']}

    def kdownload(self, db, key):
        return http.download('https://'+os.environ['KINTONE_DOMAIN']+'/k/v1/file.json',headers={'X-Cybozu-API-Token':os.environ['KINTONE_API_TOKEN_'+db.upper()]},params={'fileKey':key})

    def validate_source(self, source):
        # 配送を再開するたび原本を再取得し、削除や変更を反映する。
        return self.download(source)

    def preflight(self):
        if not self.folder:raise Held('添付保存フォルダが未設定です')
        folder=http.request('GET','https://www.googleapis.com/drive/v3/files/'+quote(self.folder,safe=''),headers=self.google(),params={'fields':'id,mimeType,trashed,capabilities(canAddChildren)','supportsAllDrives':'true'}).json()
        if folder.get('trashed') or folder.get('mimeType')!='application/vnd.google-apps.folder' or not folder.get('capabilities',{}).get('canAddChildren'):
            raise Held('Drive保存フォルダへ追加できません')
        permissions=http.request('GET','https://www.googleapis.com/drive/v3/files/'+quote(self.folder,safe='')+'/permissions',headers=self.google(),params={'fields':'permissions(type),nextPageToken','supportsAllDrives':'true'}).json()
        if permissions.get('nextPageToken') or any(p['type']=='anyone' for p in permissions.get('permissions',[])):
            raise Held('Drive保存フォルダの共有範囲を確認してください')
        fields={}
        for db in CATEGORIES:
            properties=self.kintone(db,'app/form/fields.json',params={'app':os.environ['KINTONE_APP_ID_'+db.upper()]}).json()['properties']
            code=self.config[db]['kintone_field']
            if properties.get(code,{}).get('type')!='FILE':raise Held('kintone '+db+' の添付欄 '+code+' が必要です')
            fields[db]=code
        return {'drive_folder':'checked','kintone_fields':fields,'human_drive_access':'未検証：稼働前に担当者自身でフォルダ閲覧を確認'}

    @contextmanager
    def source_guard(self, source):
        if source.tool!='gmail':
            yield lambda: None
            return
        from src.email_review.repository import Repository as Reviews
        with Reviews().locked(source.field) as (conn,row):
            owner=conn.execute('SELECT 1 FROM "EmailReviewOwner" WHERE "reviewId"=%s AND "ownerEmail"=%s AND "gmailMessageId"=%s', (source.field,source.owner,source.external_record)).fetchone()
            if not owner or row.get('state')!='done' or row.get('action_id')!=source.record:
                raise Held('メール受付または所有者が変更されています')
            self.mail_guard=source.key
            try:yield lambda: conn.execute('SELECT 1')
            finally:self.mail_guard=None
