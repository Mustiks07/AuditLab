"""Server-configured research jobs. Browser requests contain only a registered experiment id.

AUDITLAB_RESEARCH_CONTROLLER names a trusted JSON registry. Each entry specifies plan, output,
console and explicit allow_provider_calls. No secrets are stored in this file or returned to UI.
Stop is cooperative: finish the bounded current attempt, then start no further attempts.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path
from .agents.experiment import load_plan, is_paid_arm
from .agents.__main__ import preflight
from .console_export import read, write, publish


class ControllerError(ValueError):
    code='controller_error'


def configured():
    return bool(os.environ.get("AUDITLAB_RESEARCH_CONTROLLER"))


def registry():
    name=os.environ.get('AUDITLAB_RESEARCH_CONTROLLER')
    if not name:return {}
    return read(name).get('experiments',{})


def entry(ident):
    cfg=registry().get(ident)
    if not isinstance(cfg,dict):raise ControllerError('unknown registered experiment')
    plan,sha=load_plan(cfg['plan'])
    if plan['artifact_id']!=ident:raise ControllerError('registered id differs from plan')
    if any(e['split'] in ('holdout','test','final') for e in plan['episodes']):
        raise ControllerError('sealed runs are CLI-only')
    return cfg,plan,sha


def readiness(cfg,plan):
    paid=[a for a in plan['arms'] if is_paid_arm(a)]
    if not paid:return True,True
    budget=plan.get('budget') or {}
    ready=cfg.get('allow_provider_calls') is True and budget.get('max_usd_total',0)>0 and budget.get('max_usd_per_attempt',0)>0
    access=all(preflight(a['provider'])['ready_for_paid_calls'] for a in paid)
    return ready,access


def status():
    states={};budget=False;access=False
    for ident in registry():
        try:
            cfg,plan,_=entry(ident);b,a=readiness(cfg,plan);budget|=b;access|=a
            path=Path(cfg['output'])/'controller-state.json'
            states[ident]=read(path).get('status') if path.exists() else 'ready'
        except (ValueError,OSError,KeyError):states[ident]='configuration_error'
    return {'budget_configured':budget,'api_key_configured':access,'experiments':states,
            'reason':None if states else 'controller_not_configured'}


def start(ident):
    cfg,plan,_=entry(ident)
    budget,access=readiness(cfg,plan)
    if not budget or not access:raise ControllerError('server budget or provider access missing')
    output=Path(cfg['output']).resolve()
    from .agents.execute import ensure_safe_output
    ensure_safe_output(output)
    output.mkdir(parents=True,exist_ok=True)
    lock=output/'controller.lock'
    try:lock.mkdir()
    except FileExistsError:raise ControllerError('job already started; inspect saved state before CLI resume')
    write(output/'controller-state.json',{'status':'starting'})
    try:
        with (output/'controller.log').open('a',encoding='utf-8') as log:
            proc=subprocess.Popen([sys.executable,'-m','auditlab.research.controller',ident],stdout=log,stderr=log,
                                  cwd=Path(__file__).resolve().parents[2],start_new_session=True)
        return {'ok':True,'experiment_id':ident,'status':'starting'}
    except Exception:
        lock.rmdir();write(output/'controller-state.json',{'status':'failed'});raise


def stop(ident):
    cfg,plan,_=entry(ident);output=Path(cfg['output'])
    if not (output/'controller.lock').is_dir():raise ControllerError('experiment is not running')
    (output/'stop.request').touch()
    write(output/'controller-state.json',{'status':'stopping'})
    return {'ok':True,'experiment_id':ident,'status':'stopping'}


def worker(ident):
    from .agents.experiment import run_experiment,export_for_review
    from review.agent_generalization.compare import compare_experiment
    cfg,plan,sha=entry(ident);output=Path(cfg['output']);target=Path(cfg['console'])
    stop_requested=lambda:(output/'stop.request').exists()
    try:
        write(output/'controller-state.json',{'status':'running'})
        publish(plan,output,target,state='running')
        def progress(message):
            print(message,flush=True)
            publish(plan,output,target,state='stopping' if stop_requested() else 'running')
        result=run_experiment(plan,sha,output,confirm_paid=cfg.get('allow_provider_calls') is True,
                              log=progress,stop_requested=stop_requested)
        planned=len(plan['arms'])*len(plan['episodes'])*plan['repetitions']
        state='stopped' if stop_requested() else 'completed' if result['attempts_total']==planned else 'incomplete'
        review=output/'independent-review'
        export_for_review(output,review,plan)
        comparison=compare_experiment(review)
        write(output/'independent-comparison.json',comparison)
        publish(plan,output,target,comparison=comparison,state=state)
        write(output/'controller-state.json',{'status':state})
    except Exception as error:
        write(output/'controller-state.json',{'status':'failed','error':type(error).__name__})
        try:publish(plan,output,target,state='failed')
        except Exception:pass
        raise
    finally:
        # Keep the receipt. A second click cannot replay a finished paid job.
        pass

if __name__=='__main__':worker(sys.argv[1])
