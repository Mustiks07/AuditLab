"""Versioned public console artifact envelopes, independent from trusted execution records."""
import json
import math
import re
from pathlib import Path

FIELDS = {
 'experiment': {'status':'string','agents':'array','progress':'object'},
 'attempt': {'experiment_id':'string','status':'string','steps':'array'},
 'failure': {'experiment_id':'string','finding_type':'string','attempt_ids':'array'},
 'reduction': {'experiment_id':'string','original':'object','reduced':'object','repeats':'object'},
 'curriculum': {'experiment_id':'string','items':'array','splits':'object'},
 'training': {'curriculum_id':'string','kind':'string','state':'string'},
 'comparison': {'experiment_id':'string','status':'string','verification':'object','variants':'array'},
 'validation': {'independent':'object','source_book':'object','witness':'object'},
 'candidate': {'family_id':'string','split':'string','snapshot_ref':'string'},
}
TYPES={'string':str,'array':list,'object':dict}
ID=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$'


def schema(kind):
    properties={'schema_version':{'const':'1.0'},'artifact_kind':{'const':kind},
        'artifact_id':{'type':'string','pattern':ID},'source_sha':{'type':'string','minLength':1},
        'protocol_id':{'type':'string','minLength':1},'provenance':{'type':'object','required':['kind'],
        'properties':{'kind':{'enum':['measured','test_double','dev_fixture']}}}}
    properties.update({k:{'type':v} for k,v in FIELDS[kind].items()})
    return {'$schema':'https://json-schema.org/draft/2020-12/schema',
            '$id':f'https://github.com/Mustiks07/AuditLab/schemas/research/{kind}-1.0.json',
            'title':f'AuditLab public research {kind}', 'type':'object',
            'required':list(properties),'properties':properties,'additionalProperties':True}


def validate(data):
    kind=data.get('artifact_kind')
    if kind not in FIELDS:raise ValueError('unknown research artifact kind')
    if data.get('schema_version')!='1.0':raise ValueError('unsupported research artifact schema')
    if not isinstance(data.get('artifact_id'),str) or not re.fullmatch(ID,data['artifact_id']):raise ValueError('invalid artifact id')
    for field in ('source_sha','protocol_id'):
        if not isinstance(data.get(field),str) or not data[field]:raise ValueError('missing '+field)
    if (data.get('provenance') or {}).get('kind') not in ('measured','test_double','dev_fixture'):raise ValueError('missing provenance')
    for key,typename in FIELDS[kind].items():
        if not isinstance(data.get(key),TYPES[typename]):raise ValueError('invalid '+key)
    def walk(value):
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('non-finite number')
        if isinstance(value,dict):
            for key,item in value.items():
                if key.lower() in ('api_key','secret','password','authorization','truth','private','generator_config','seed'):raise ValueError('private field in public artifact')
                walk(item)
        elif isinstance(value,list):
            for item in value:walk(item)
    walk(data)
    return data
