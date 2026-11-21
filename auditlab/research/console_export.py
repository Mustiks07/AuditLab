"""Explicit trusted-to-public export; the web server never scans experiment working files."""
from __future__ import annotations
import copy
import json
import shutil
from pathlib import Path
from auditlab.research.agents.experiment import _load_attempts
from auditlab.research.agents.protocol import get_protocol
from auditlab.research.agents.execute import source_sha


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    if isinstance(value, dict) and value.get("artifact_kind"):
        from .artifacts import validate
        validate(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    temp.replace(path)


def publish(plan, output, target, *, comparison=None, state=None):
    """Publish completed attempt evidence and optionally an independently computed comparison.

    Holdout/test episodes are never exported. No provider config, transcript, private path or key
    is copied. Only explicit public snapshot files are copied, rejecting symlinks.
    """
    if any(e['split'] in ('holdout', 'test', 'final') for e in plan['episodes']):
        raise ValueError('sealed episodes cannot be published to the console')
    output, target = Path(output), Path(target)
    if target.resolve() == output.resolve() or output.resolve() in target.resolve().parents:
        raise ValueError('console export must be separate from trusted output')
    rows = _load_attempts(output)
    eid = plan['artifact_id']
    proto = get_protocol(plan['protocol_id'])
    base = {'schema_version':'1.0', 'source_sha':source_sha(), 'protocol_id':plan['protocol_id'],
            'provenance':{'kind':'measured', 'note':'Фактический запуск; локальные политики и API-модели указаны отдельно.'}}
    def artifact(kind, ident, **fields):
        return {**copy.deepcopy(base), 'artifact_kind':kind, 'artifact_id':ident, **fields}
    for row in rows:
        aid = row['artifact_id']
        ep = row['episode']
        public = Path(ep['episode_ref'])/'public'
        ref = f'snapshots/{aid}/public'
        dest = target/ref
        dest.mkdir(parents=True, exist_ok=True)
        for name in ('manifest.json','policy.json','ledger.csv','invoices.jsonl','payments.jsonl','fx.csv'):
            src = public/name
            if src.is_symlink() or public.resolve() not in src.resolve().parents:
                raise ValueError('unsafe public snapshot')
            shutil.copyfile(src, dest/name)
        steps = []
        steps_path = output/'attempts'/aid/'steps.jsonl'
        for raw in steps_path.read_text(encoding='utf-8').splitlines() if steps_path.exists() else []:
            step = json.loads(raw); action = step['action']; obs = step['observation']
            args = action.get('args') or {}
            if action.get('name') == 'submit': args = args.get('findings', {})
            steps.append({'step':step['index'], 'action':{'action':action.get('name'), 'args':args},
                          'observation':{'ok':obs.get('error') is None, 'result':obs.get('result'),
                                         'error':obs.get('error'), 'remaining_steps':obs.get('remaining_steps')}})
        ev = (row.get('_evaluation') or {}).get('official')
        evaluation = None if ev is None else {'visibility':'open', 'tp':ev['tp'], 'fp':ev['fp'], 'fn':ev['fn'],
                                               'reward':ev['return'], 'inspection_steps':row.get('inspection_steps')}
        totals = row.get('totals') or {}; tokens = totals.get('tokens') or {}
        kind = 'test_double' if row.get('test_double') else 'measured'
        attempt = artifact('attempt', aid, experiment_id=eid, variant_id=row.get('variant'),
                           repetition=row.get('repetition'), status=row['status'], snapshot_ref=ref,
                           episode={k:ep.get(k) for k in ('family_id','split')}, evaluation=evaluation,
                           submission=row.get('final_answer'), steps=steps,
                           usage={'input_tokens':tokens.get('input'), 'output_tokens':tokens.get('output'),
                                  'cost_usd':totals.get('cost_usd'), 'elapsed_s':totals.get('wall_seconds')},
                           started_at=row.get('started_at'), finished_at=row.get('finished_at'))
        attempt['provenance']['kind'] = kind
        write(target/'attempts'/f'{aid}.json', attempt)
    counts = {}
    for row in rows: counts[row['status']] = counts.get(row['status'],0)+1
    planned = len(plan['arms'])*len(plan['episodes'])*plan['repetitions']
    agents = [{'variant_id':a['name'], 'label':a.get('label',a['name']), 'kind':a.get('agent',{}).get('kind','llm'),
               'model':{'provider':a.get('provider',{}).get('provider'), 'model_id':a.get('provider',{}).get('model')}} for a in plan['arms']]
    spent = None if any(r['totals'].get('cost_usd') is None for r in rows) else sum(r['totals']['cost_usd'] for r in rows)
    write(target/'experiment.json', artifact('experiment',eid,title=plan.get('title','Проверка финансовых аудиторов'),
        status=state or ('completed' if len(rows)>=planned else 'incomplete'), agents=agents,
        mode={'protocol_id':proto.protocol_id,'official':proto.official,'action_limit':100,'time_limit_s':proto.time_limit_s},
        progress={'planned':planned,'completed':counts.get('completed',0),'running':0,
                  'failed':{k:v for k,v in counts.items() if k!='completed'}},
        repetitions=plan['repetitions'], attempt_ids=[r['artifact_id'] for r in rows],
        budget={'currency':'USD','limit':plan.get('budget',{}).get('max_usd_total'),'spent':spent},
        cost={'usd':spent}, split={'rule':'Исходное разбиение зафиксировано в плане; открытая выборка.'}))
    if comparison is not None:
        variants=[]
        for name, m in comparison['arms'].items():
            variants.append({'variant_id':name,'label':name,'metrics':{
                'micro_f1':m['micro_f1']['value'], 'mean_reward':m['mean_reward']['value'],
                'clean_fp':{'count':m['fp_on_clean']['attempts_with_findings'],'denominator':m['fp_on_clean']['clean_attempts']},
                'completion':{'completed':m['completion']['submitted'],'total':m['completion']['attempts']},
                'actions_mean':m['actions']['mean'],'time_s':m['wall_time_s']['mean'],
                'cost_usd':None if m['cost_usd']['n']!=m['attempts'] else (m['cost_usd']['mean'] or 0)*m['attempts']}})
        pairs=[{'a':v['a'],'b':v['b'],'metric':'mean_reward','difference':v['difference'],'interval':v['ci95'],
                'method':'Парный bootstrap по экономическим семействам'} for v in comparison['paired_by_family'].values() if v['difference'] is not None]
        write(target/'comparison.json',artifact('comparison',f'comparison-{eid}', experiment_id=eid,
            title='Независимое сравнение аудиторов',status='completed', verification={'status':'passed'},
            verified_by='review.agent_generalization.compare', sample={'families':comparison['families'],
            'episodes':comparison['episodes'],'repetitions':plan['repetitions'],'note':'Внутренняя открытая проверка, не конкурсный балл.'},
            variants=variants,paired=pairs,missing=[]))
    return {'experiment_id':eid,'attempts':len(rows),'target':str(target)}


def publish_curriculum(output, target):
    """Project measured GPT 1 search/reduction and Claude 2 receipts, never the sealed partition."""
    import hashlib
    root,target=Path(output).resolve(),Path(target).resolve()
    exp=read(root/'experiment.json'); red=read(root/'reduction/reduction.json'); failure=read(root/'failure.json')
    cur=read(root/'curriculum/curriculum.json'); eid=exp['artifact_id'];rid=red['artifact_id'];fid=failure['artifact_id']
    members={m['candidate_id']:m for m in exp['partition']['members']}
    if members[failure['candidate_id']]['split'] not in ('train','dev'):raise ValueError('sealed failure')
    base={'schema_version':'1.0','source_sha':exp['source_sha'],'protocol_id':exp['protocol_id'],
          'provenance':{'kind':'measured','note':'Повторный запуск встроенной политики; не результат ИИ-модели.'}}
    def make(artifact_kind,ident,**fields):return {**copy.deepcopy(base),'artifact_kind':artifact_kind,'artifact_id':ident,**fields}
    def safe(path):
        p=Path(path).resolve()
        if root not in p.parents:raise ValueError('reference outside experiment')
        return p
    def snapshot(path,label):
        public=safe(path)/'public';dest=target/'snapshots'/label/'public';dest.mkdir(parents=True,exist_ok=True)
        for name in ('manifest.json','policy.json','ledger.csv','invoices.jsonl','payments.jsonl','fx.csv'):
            src=public/name
            if src.is_symlink():raise ValueError('symlink snapshot')
            shutil.copyfile(src,dest/name)
        return f'snapshots/{label}/public'
    selected=next(p for p in red['proposals'] if p['snapshot']==red['selected_snapshot'])
    attempt_ids=[]; sides={};repeats={}
    for label,part in [('original',red['baseline']),('reduced',selected)]:
        ref=snapshot(part['snapshot'],label);validation=part['validation'];vid=f'{rid}-{label}-validation'
        answer_hash=validation['answer_sha256_before_truth']
        answers=[p for p in root.rglob('answer_before_truth.json') if hashlib.sha256(p.read_bytes()).hexdigest()==answer_hash]
        if not answers:raise ValueError('missing independently committed answer')
        answer=read(answers[0])
        write(target/'validations'/f'{vid}.json',make('validation',vid,snapshot_ref=ref,
            independent={'engine':'Независимый финансовый расчёт','status':validation['status'],'findings':answer['findings']},
            source_book=validation['checks']['source_book_consistent'],witness=validation['checks']['witness_trajectory']))
        aids=[]
        for item in part['attempts']:
            receipt=item['receipt'];aid=receipt['attempt_id'];aids.append(aid);attempt_ids.append(aid)
            directory=safe(item['output_dir'])/'runner'/aid
            steps=[json.loads(l) for l in (directory/'trajectory.jsonl').read_text().splitlines() if l.strip()]
            report=read(directory/'report.json');score=report['evaluation']['score']
            submit=next((s['action']['args'] for s in reversed(steps) if s['action']['action']=='submit'),None)
            write(target/'attempts'/f'{aid}.json',make('attempt',aid,experiment_id=eid,variant_id=exp['agent']['name'],
                status='completed' if receipt['status']=='submitted' else receipt['status'],repetition=item['repetition'],
                snapshot_ref=ref,episode={'split':members[failure['candidate_id']]['split'],'family_id':members[failure['candidate_id']]['family_id']},
                evaluation={'visibility':'open',**{k:score[k] for k in ('tp','fp','fn','reward')},'inspection_steps':report['inspection_steps']},
                submission=submit,steps=steps,usage={'cost_usd':receipt['cost_micros']/1e6,'elapsed_s':receipt['elapsed_seconds']}))
        sides[label]={'snapshot_ref':ref,'validation_id':vid,**{k:part['sizes'][k] for k in ('ledger_rows','documents')}}
        repeats[label]={'failed':part['frequency']['matched'],'total':part['frequency']['attempted'],'attempt_ids':aids}
    frequency=red['baseline']['frequency'];interval=frequency['per_attempt']['wilson_95']
    write(target/'experiment.json',make('experiment',eid,title='Поиск и сокращение финансовой ошибки',status=exp['status'],
        mode={'protocol_id':exp['budget']['episode_protocol_id'],
              'official':exp['budget']['episode_protocol_id']=='genesis-official-60s',
              'action_limit':exp['budget']['actions_per_episode'],'time_limit_s':exp['budget']['seconds_per_episode']},
        agents=[{'variant_id':exp['agent']['name'],'label':'Случайный аудитор','kind':'baseline_policy'}],
        attempt_ids=attempt_ids,progress={'planned':len(attempt_ids),'completed':len(attempt_ids),'running':0},
        repetitions=red['budget']['repetitions'],budget={'currency':'USD','limit':0,'spent':red['cost']['cost_micros']/1e6}))
    write(target/'failure.json',make('failure',fid,experiment_id=eid,title='Пропущенное распределение платежа',
        finding_type=failure['target']['issue_type'],variant_id=exp['agent']['name'],
        family_id=members[failure['candidate_id']]['family_id'],attempt_ids=repeats['original']['attempt_ids'],
        validation_id=sides['original']['validation_id'],reduction_id=rid,
        criterion={'text':'Аудитор не обнаружил подтверждённую ошибку распределения платежа.'},
        frequency={'failed':frequency['matched'],'total':frequency['attempted']},
        interval={'low':interval[0],'high':interval[1],'method':'Уилсон, 95%'},alternatives=['Проверена встроенная случайная политика; перенос на ИИ-модель не измерен.']))
    write(target/'reduction.json',make('reduction',rid,experiment_id=eid,failure_id=fid,**sides,repeats=repeats,families=[],
        method='Удаление групп с независимой проверкой и новыми расследованиями',
        transformations=['Удалены не связанные с ошибкой записи и документы; ошибка подтверждена повторно.' for p in red['proposals'] if p['accepted']]))
    items=[];filtered=[];splits={}
    for item in cur['examples']:
        if item['split'] not in ('train','dev'):raise ValueError('sealed curriculum item')
        if item['status']!='included':
            filtered.append({'title':'Повтор оформления','reason':item.get('reason')});continue
        splits[item['split']]=splits.get(item['split'],0)+1
        provenance=item.get('provenance') or {}
        origin=('reduction' if provenance.get('reduction') else 'family_transform' if item.get('parent')
                else 'clean_control' if provenance.get('selection')=='predeclared_clean_control'
                else 'failure' if provenance.get('selection')=='observed_agent_failure' else 'search')
        items.append({'item_id':item['candidate_id'],'title':'Финансовый случай '+str(len(items)+1),
            'split':item['split'],'family_id':item['family_id'],'hash':item['snapshot_sha256'],
            'validation':item['validation']['status'],'origin':origin})
    write(target/'curriculum.json',make('curriculum',cur['artifact_id'],experiment_id=eid,title='Проверенный учебный набор',
        failure_ids=[fid],reduction_ids=[rid],items=items,filtered=filtered,splits=splits,export_formats=['SFT'],split_hash=exp['partition']['partition_sha256']))
    write(target/'training.json',make('training','training-'+eid,curriculum_id=cur['artifact_id'],kind='weights',
        state='not_available',result=None,error={'code':'not_checked','message':'Обучение весов не запускалось: модель и GPU не настроены.'}))
    write(target/'comparison.json',make('comparison','comparison-'+eid,experiment_id=eid,title='Эффект обучения ещё не измерен',
        status='not_checked',verification={'status':'not_checked','reason':'Нет обученных моделей и финального эксперимента.'},
        variants=[],paired=[],missing=[{'variant_id':'weights','reason':'Обучение весов и независимый финальный прогон не выполнены.'}]))
    return {'experiment_id':eid,'attempts':len(attempt_ids),'target':str(target)}
