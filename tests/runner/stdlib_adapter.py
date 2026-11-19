"""External API-05 fixture: only stdlib + documented JSON Lines, no AuditLab imports."""
import json
import sys
from decimal import Decimal


def read():
    value = json.loads(sys.stdin.readline())
    assert isinstance(value, dict)
    assert type(value['remaining_steps']) is int
    return value


def inspect(action, **args):
    print(json.dumps({'action': action, 'args': args}), flush=True)
    response = read()
    assert type(response['ok']) is bool
    assert set(response) == {'ok', 'result', 'remaining_steps'}
    assert response['ok'] is True
    return response['result']


initial = read()
assert initial['remaining_steps'] == 100
assert set(initial) == {'task', 'tools', 'manifest', 'policy', 'remaining_steps'}
assert isinstance(initial['task'], str)
assert set(initial['tools']) == {'list_records', 'get_record', 'get_document', 'calculator', 'submit'}
manifest = initial['manifest']
assert manifest['schema_version'] == '1.0'
assert isinstance(manifest['episode_id'], str)
page = inspect('list_records', table='payments', cursor=None, limit=50)
assert set(page) == {'records', 'next_cursor'}
assert isinstance(page['records'], list) and len(page['records']) <= 50
payment = page['records'][0]
assert inspect('get_document', doc_id=payment['doc_id']) == payment
rule = inspect('get_record', table='policy', id='allocation')
assert isinstance(rule, dict) and isinstance(rule['rule'], str)
credit = debit = 0
evidence = ['payments/' + payment['doc_id']]
for row_id in payment['settlement_index']['unapplied_row_ids']:
    row = inspect('get_record', table='ledger', id=row_id)
    assert row['row_id'] == row_id and row['payment_id'] == payment['payment_id']
    assert row['currency'] == payment['currency']
    assert type(row['credit_minor']) is int and type(row['debit_minor']) is int
    credit += row['credit_minor']
    debit += row['debit_minor']
    evidence.append('ledger/' + row_id)
amount = inspect('calculator', expression=f'{credit}-{debit}')
assert Decimal(amount) == credit - debit
print(json.dumps({'action': 'get_document', 'args': {'doc_id': 'API05-NONEXISTENT'}}), flush=True)
error = read()
assert error['ok'] is False and set(error) == {'ok', 'error', 'remaining_steps'}
assert set(error['error']) == {'code', 'message'}
assert error['error']['code'] == 'not_found' and isinstance(error['error']['message'], str)
findings = []
if Decimal(amount) > 0:
    findings = [{'type': 'payment_allocation',
                 'affected_ids': ['payment/' + payment['payment_id'], 'invoice/' + payment['invoice_refs'][0]],
                 'claimed_amount_minor': int(Decimal(amount)), 'currency': payment['currency'],
                 'evidence_ids': evidence + ['policy/allocation'],
                 'explanation': 'Complete public settlement index: credits minus debits before the boundary.'}]
print(json.dumps({'action': 'submit', 'args': {'episode_id': manifest['episode_id'], 'findings': findings}}), flush=True)
