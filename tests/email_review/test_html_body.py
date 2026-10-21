"""HTML・長文・別取得本文を、外部通信なしで全文照合する。"""
import base64
from copy import deepcopy
import pytest
from src.email_review.mime import plain_text, hydrate_body, validate_payload, within_limit
from src.email_review.gateway import Gateway


def part(text, kind='text/html', **extra):
    return {'mimeType': kind, 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}, **extra}


def test_html_only_preserves_lines_signature_and_entities_without_hidden_or_quote():
    html = '<html><head><style>hidden</style></head><body><div>試験株式会社</div><p>田中様<br>ご提案 &amp; ご相談</p><div>署名<br>担当</div><script>秘密</script><div hidden>非表示</div><div style="display: none">隠す</div><blockquote>以前の別会社</blockquote><div class="gmail_quote">過去</div></body></html>'
    body = plain_text(part(html))
    assert body == '試験株式会社\n\n田中様\nご提案 & ご相談\n\n署名\n担当'
    assert all(value not in body for value in ['秘密', '非表示', '隠す', '別会社', '過去', 'hidden'])


def test_multipart_alternative_prefers_plain_without_html_duplication():
    payload = {'mimeType':'multipart/alternative', 'parts':[part('本文', 'text/plain'), part('<p>本文</p>')]}
    assert plain_text(payload) == '本文'
    assert plain_text(part('<p>添付</p>',filename='note.html')) == ''
    assert plain_text({'mimeType':'message/rfc822','parts':[part('<p>転送</p>')]}) == ''


@pytest.mark.parametrize('body', ['長' * 40000, '😀' * 20000])
def test_long_body_preserved_in_history_and_notion_chunks(body):
    assert within_limit(body)
    assert plain_text(part(body,'text/plain')) == body
    row = {'id':'synthetic','direction':'outbound','contact_name':'相手','company_id':'company',
           'message':{'label_ids':['SENT'],'body_text':body,'internal_date_ms':'1792130400000'}}
    assert Gateway().action_properties(row)['履歴メモ'] == body
    from src.sync_engine.clients.notion_client import _rich_text_content
    assert ''.join(item['text']['content'] for item in _rich_text_content(body)) == body
    assert plain_text(part(body+'x','text/plain')) == ''


def test_external_body_fetches_only_body_and_does_not_mutate_original():
    payload={'mimeType':'multipart/mixed','parts':[
        {'mimeType':'text/html','body':{'attachmentId':'body','size':100}},
        {'filename':'file.html','mimeType':'text/html','body':{'attachmentId':'file','size':100}},
        {'mimeType':'message/rfc822','parts':[{'mimeType':'text/plain','body':{'attachmentId':'forward','size':100}}]}
    ]}
    old=deepcopy(payload);calls=[]
    def fetch(identifier):
        calls.append(identifier)
        return part('<p>本文</p>')['body']
    assert plain_text(hydrate_body(payload,fetch)) == '本文'
    assert calls == ['body'] and payload == old


def test_unfetched_body_never_becomes_partial_full_text():
    payload={'mimeType':'multipart/mixed','parts':[part('前半','text/plain'),{'mimeType':'text/plain','body':{'attachmentId':'large','size':150001}}]}
    assert plain_text(hydrate_body(payload,lambda _:pytest.fail('容量超過を取得してはいけない'))) == ''


@pytest.mark.parametrize('bad', [[], {'body':[],'mimeType':5}, {'body':{'data':12}}, {'parts':[{}]*31}])
def test_malformed_mime_rejected(bad):
    with pytest.raises(ValueError): validate_payload(bad)


def test_deep_and_oversized_mime_is_held():
    payload=part('本文')
    for _ in range(22): payload={'mimeType':'multipart/mixed','parts':[payload]}
    assert plain_text(payload) == ''
    assert plain_text(part('x'*200001)) == ''


def test_mixed_independent_html_is_preserved_and_missing_html_holds_all():
    payload={'mimeType':'multipart/mixed','parts':[part('前半','text/plain'),part('<p>後半</p>')]}
    assert plain_text(payload)=='前半\n後半'
    payload['parts'][1]={'mimeType':'text/html','body':{'attachmentId':'missing','size':100}}
    assert plain_text(payload)==''


def test_nested_alternative_does_not_duplicate_body():
    payload={'mimeType':'multipart/alternative','parts':[part('本文','text/plain'),{'mimeType':'multipart/related','parts':[part('<p>本文</p>'),{'mimeType':'image/png'}]}]}
    assert plain_text(payload)=='本文'


def test_links_keep_http_destination_without_executing_other_schemes():
    assert plain_text(part('<p>資料は<a href="https://example.invalid/doc">こちら</a>です。</p>')) == '資料はこちら（https://example.invalid/doc）です。'
    assert plain_text(part('<a href="javascript:alert(1)">資料</a>')) == '資料'
    assert plain_text(part('<a href="https://example.invalid/doc">https://example.invalid/doc</a>')) == 'https://example.invalid/doc'


def test_hydration_limit_holds_but_transport_failure_retries():
    payload={'mimeType':'text/plain','body':{'attachmentId':'body','size':100}}
    assert plain_text(hydrate_body(payload, lambda _: {'data':'x'*250001})) == ''
    def failed(_): raise RuntimeError('一時的な本文取得失敗')
    with pytest.raises(RuntimeError): hydrate_body(payload,failed)
    deep=payload
    for _ in range(22):deep={'mimeType':'multipart/mixed','parts':[deep]}
    assert plain_text(hydrate_body(deep,lambda _:pytest.fail('構造超過の本文を取得しない'))) == ''
