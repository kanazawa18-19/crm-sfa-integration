from src.sync_operations.product_holds import is_held


def test_only_real_holds_are_resumable():
    snapshot = {'task': {'evaluationHeld': False}, 'pairs': [], 'deliveries': []}
    assert not is_held(snapshot)
    snapshot['task']['evaluationHeld'] = True
    assert is_held(snapshot)
    snapshot['task']['evaluationHeld'] = False
    snapshot['pairs'] = [{'state': 'held'}]
    assert is_held(snapshot)
    snapshot['pairs'] = [{'state': 'done'}]
    snapshot['deliveries'] = [{'state': 'held'}]
    assert is_held(snapshot)


def test_hold_hash_ignores_only_scheduling_counters():
    from copy import deepcopy
    from src.sync_operations.product_holds import hold_snapshot_hash
    snapshot = {'task': {'projectId':'p','evaluationHeld':True,'attempts':1,'updatedAt':'old','nextAttemptAt':'old'},
                'pairs':[{'state':'held','lastError':'MAPPING_MISSING'}],
                'deliveries':[{'state':'held','verificationAttempts':8,'expectedIds':['p']} ]}
    current = deepcopy(snapshot)
    current['task'].update(attempts=2,updatedAt='new',nextAttemptAt='new')
    current['deliveries'][0]['verificationAttempts'] = 9
    assert hold_snapshot_hash(current) == hold_snapshot_hash(snapshot)
    current['deliveries'][0]['expectedIds'] = ['other']
    assert hold_snapshot_hash(current) != hold_snapshot_hash(snapshot)
