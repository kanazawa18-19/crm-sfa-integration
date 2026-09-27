from types import SimpleNamespace
import pytest
from src.hub_creation.sheet_gateway import SheetRegistrationGateway
from src.hub_creation.domain import CreationHeld


class Client:
    key='new:abc'
    duplicates=False
    def __init__(self): self.calls=[]; self.matches=1
    def _grid_properties(self,sheet): return {'sheetId':4}
    def _get_header_row(self,sheet): return ['グループ名','同期キー','登録状況']
    def find_unique_row_by_sync_key(self,*args): return None if self.duplicates else 51
    def _request(self,method,path,json_body):
        self.calls.append((path,json_body))
        if path.endswith('Metadata:search'):
            data={'matchedDeveloperMetadata':[{'developerMetadata':{'metadataId':123,'location':{'dimensionRange':{'sheetId':4,'dimension':'ROWS','startIndex':50,'endIndex':51}}}}]*self.matches}
        elif path.endswith('batchGetByDataFilter'):
            data={'valueRanges':[{'valueRange':{'values':[['グループ',self.key,'受付']]}}]}
        else: data={'totalUpdatedRows':1}
        return SimpleNamespace(ok=True,status_code=200,json=lambda:data)


def test_key_replacement_uses_row_metadata_and_keeps_other_cells():
    client=Client(); gateway=SheetRegistrationGateway(client)
    gateway.update('チェーン','new:abc',notion_key='notion-page',status='完了')
    path,body=client.calls[-1]
    assert path=='/values:batchUpdateByDataFilter'
    assert body['data'][0]['dataFilter']=={'developerMetadataLookup':{'metadataId':123}}
    assert body['data'][0]['values']==[[None,'notion-page','完了']]
    assert 'range' not in body['data'][0]


@pytest.mark.parametrize('matches',[0,2])
def test_missing_or_duplicated_metadata_stops_before_writing(matches):
    client=Client(); client.matches=matches
    with pytest.raises(CreationHeld): SheetRegistrationGateway(client).update('チェーン','new:abc',status='保留')
    assert len(client.calls)==1


def test_copied_key_stops_before_writing():
    client=Client(); client.duplicates=True
    with pytest.raises(CreationHeld): SheetRegistrationGateway(client).update('チェーン','new:abc',status='保留')
    assert not any(path.endswith('batchUpdateByDataFilter') for path,body in client.calls)


def test_changed_key_stops_before_writing():
    client=Client(); client.key='other'
    with pytest.raises(CreationHeld): SheetRegistrationGateway(client).update('チェーン','new:abc',status='保留')
    assert not any(path.endswith('batchUpdateByDataFilter') for path,body in client.calls)
