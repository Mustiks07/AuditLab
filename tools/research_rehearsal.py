"""Reproduce the integrated research path with real local policies; no API or training charges."""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from auditlab.research.console_export import read,write,publish,publish_curriculum
from auditlab.research.curriculum.validation import REVIEWER_SHA,IndependentValidator
from auditlab.research.curriculum.agent_bridge import CLAUDE_RUNNER_SHA
from auditlab.research.curriculum.laboratory import local_runner,run_experiment
from auditlab.research.curriculum.prepare_teacher import prepare_teacher
from auditlab.research.agents import experiment
from review.agent_generalization.compare import compare_experiment


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    output=a.output.resolve();output.mkdir(parents=True,exist_ok=False)
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    worktrees=[]
    try:
        for name,revision in [('reviewer',REVIEWER_SHA),('teacher',CLAUDE_RUNNER_SHA)]:
            path=output/name
            subprocess.run(['git','worktree','add','--detach',str(path),revision],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
            worktrees.append(path)
        plan=read(ROOT/'docs/research/financial-curriculum/local-plan.json')
        result=run_experiment(plan,sha,IndependentValidator(output/'reviewer'),local_runner('random',sha,plan['protocol_id']),
                              output/'search',agent_metadata={'kind':'built_in_policy','name':'random','provider_calls':0,'network':False})
        if result['status']!='completed':raise RuntimeError('search incomplete')
        prepare_teacher(output/'teacher',output/'search/curriculum/curriculum.json',output/'teacher-preparation')
        publish_curriculum(output/'search',output/'console-search')
        baseline={'schema_version':'1.0','artifact_id':'integrated-open-dev','protocol_id':'genesis-official-60s','purpose':'selection',
            'transport':'in_process','repetitions':1,'episodes':[{'episode_ref':str(m.parent.parent),'family_id':'origin-'+hashlib.sha256(str(read(m.parent.parent/'private/provenance.json')['seed']).encode()).hexdigest()[:16],'split':'dev'}
            for m in sorted((ROOT/'datasets/open/dev').glob('*/public/manifest.json'))],
            'arms':[{'name':n,'agent':{'kind':'baseline_policy','policy':n}} for n in ['empty','random','flag_all','reconcile']],
            'analysis':{'pairs':[['reconcile','empty']]}}
        write(output/'baseline-plan.json',baseline);baseline,digest=experiment.load_plan(output/'baseline-plan.json')
        summary=experiment.run_experiment(baseline,digest,output/'baselines')
        experiment.export_for_review(output/'baselines',output/'independent-review',baseline)
        comparison=compare_experiment(output/'independent-review');write(output/'comparison.json',comparison)
        publish(baseline,output/'baselines',output/'console-baselines',comparison=comparison)
        receipt={'source_sha':sha,'network_calls':0,'api_agent':'not_checked','weight_training':'not_checked','sealed_holdout':'not_checked',
            'baseline_attempts':summary['attempts_total'],'independent_comparison':comparison,
            'search_status':result['status'],'curriculum':read(output/'search/curriculum/curriculum.json')['artifact_id'],
            'sft':read(output/'teacher-preparation/sft/dataset_manifest.json')['counts']}
        write(output/'receipt.json',receipt)
        print(json.dumps({k:v for k,v in receipt.items() if k!='independent_comparison'},ensure_ascii=False))
    finally:
        for path in reversed(worktrees):subprocess.run(['git','worktree','remove',str(path)],cwd=ROOT,check=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
