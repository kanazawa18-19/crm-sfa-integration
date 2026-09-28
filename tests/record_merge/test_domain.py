import pytest
from src.record_merge.domain import MergeHeld, choose_properties, replace_relation, copy_block, resumable_value


def test_conflicting_values_require_explicit_choice_and_relations_keep_all():
    with pytest.raises(MergeHeld, match='残す値'):
        choose_properties({'名前': '元'}, {'名前': '先'}, {})
    assert choose_properties({'名前': '元', '電話': '123', '関連': ['a', 'b']},
        {'名前': '先', '電話': '', '関連': ['b', 'c']}, {'名前': 'target'}, union_fields={'関連'}) == {
            '名前': '先', '電話': '123', '関連': ['b', 'c', 'a']}


def test_child_relation_preserves_unrelated_ids_and_deduplicates():
    assert replace_relation(['other', 'source', 'target'], 'source', 'target') == ['other', 'target']


def test_block_copy_strips_readonly_ids_and_holds_expiring_attachments():
    result = copy_block({'id': 'old', 'type': 'paragraph', 'paragraph': {
        'rich_text': [{'type': 'text', 'text': {'content': '本文'}, 'plain_text': '本文'}]}})
    assert 'id' not in result and 'plain_text' not in result['paragraph']['rich_text'][0]
    with pytest.raises(MergeHeld, match='添付'):
        copy_block({'type': 'file', 'file': {'type': 'file', 'file': {'url': 'temporary'}}})
    with pytest.raises(MergeHeld, match='全件'):
        copy_block({'type': 'toggle', 'has_children': True, 'toggle': {'rich_text': []}})


def test_resume_does_not_overwrite_changed_value():
    assert resumable_value('desired', 'before', 'desired') is False
    assert resumable_value('before', 'before', 'desired') is True
    with pytest.raises(MergeHeld):
        resumable_value('edited', 'before', 'desired')


def test_block_limit_counts_nested_content_before_write():
    from src.record_merge.domain import block_count
    from src.record_merge.notion_gateway import NotionMergeGateway
    nested = {'type': 'paragraph', 'paragraph': {'children': [{'type': 'divider', 'divider': {}} for _ in range(59)]}}
    assert block_count([nested, nested]) == 120
    with pytest.raises(MergeHeld, match='100ブロック'):
        NotionMergeGateway({}, None).plan({'dbKey': 'client_master', 'sourceId': 's', 'targetId': 't',
                                         'sourceBlocks': [nested], 'targetBlocks': [nested]}, {})


def test_abandon_hash_ignores_rotating_notion_file_urls_but_keeps_edit_revision():
    from src.record_merge.domain import stable_blocks, digest
    block = {'id':'attachment','type':'file','last_edited_time':'v1','file':{'type':'file','file':{'url':'https://signed.invalid/first'}}}
    before = digest(stable_blocks([block]))
    block['file']['file']['url'] = 'https://signed.invalid/second'
    assert digest(stable_blocks([block])) == before
    block['last_edited_time'] = 'v2'
    assert digest(stable_blocks([block])) != before
