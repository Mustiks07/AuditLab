"""Unified research CLI. Network and training remain explicit, budgeted operations."""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {'run':'run','validate-plan':'dry-run','smoke':'smoke','preflight':'preflight','plan':'plan','summarize':'summarize','export-review':'export-review'}
    if argv and argv[0] in commands:
        from .agents.__main__ import main as agents
        return agents([commands[argv[0]],*argv[1:]])
    if argv and argv[0] in ('train','build-curriculum'):
        from .training.__main__ import main as training
        return training([{'train':'train','build-curriculum':'build-sft'}[argv[0]],*argv[1:]])
    if argv and argv[0]=='search':
        from .curriculum.__main__ import main as search
        old=sys.argv;sys.argv=[old[0],*argv[1:]]
        try:return search()
        finally:sys.argv=old
    parser=argparse.ArgumentParser(description=__doc__,epilog='Also: '+', '.join([*commands,'search','train','build-curriculum']))
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('compare');p.add_argument('--experiment',required=True);p.add_argument('--output',required=True)
    p=sub.add_parser('publish-curriculum');p.add_argument('--output',required=True);p.add_argument('--target',required=True)
    p=sub.add_parser('publish-families');p.add_argument('--output',required=True);p.add_argument('--target',required=True)
    p=sub.add_parser('publish');p.add_argument('--plan',required=True);p.add_argument('--output',required=True);p.add_argument('--target',required=True);p.add_argument('--comparison')
    p=sub.add_parser('replay');p.add_argument('--experiment',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    from .console_export import read,write,publish
    if args.command=='publish-families':
        from .console_export import publish_families
        result=publish_families(args.output,args.target)
    elif args.command=='publish-curriculum':
        from .console_export import publish_curriculum
        result=publish_curriculum(args.output,args.target)
    elif args.command in ('compare','replay'):
        from review.agent_generalization.compare import compare_experiment
        if Path(args.output).exists(): raise FileExistsError(args.output)
        result=compare_experiment(args.experiment);write(args.output,result)
    else:
        from .agents.experiment import load_plan
        plan,_=load_plan(args.plan)
        result=publish(plan,args.output,args.target,comparison=read(args.comparison) if args.comparison else None)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
