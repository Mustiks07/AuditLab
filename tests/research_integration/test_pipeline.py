import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from auditlab.research.agents import experiment
from auditlab.research.agents.providers import ProviderError,ProviderRequest
from auditlab.research.agents.reliability import ModelCaller,RetryPolicy,SpendGuard
from auditlab.research.console_export import publish,write
from auditlab.research import controller
from auditlab.demo.research import ResearchStore,Projection
from review.agent_generalization.compare import compare_experiment

ROOT=Path(__file__).resolve().parents[2]


def plan():
    return {'schema_version':'1.0','artifact_id':'integrated-check','protocol_id':'genesis-official-60s',
            'purpose':'selection','repetitions':1,'transport':'in_process',
            'episodes':[{'episode_ref':str(ROOT/'fixtures/foundation'/s),'family_id':'boundary-pair','split':'dev'} for s in ('S01','S02')],
            'arms':[{'name':n,'agent':{'kind':'baseline_policy','policy':n}} for n in ('empty','reconcile')],
            'analysis':{'pairs':[['reconcile','empty']]}}


class IntegrationTests(unittest.TestCase):
    def test_real_runner_independent_comparison_console_and_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);p=plan();experiment.validate_plan(p)
            result=experiment.run_experiment(p,'test-plan',base/'run',log=lambda _:None)
            self.assertEqual(result['attempts_total'],4)
            experiment.export_for_review(base/'run',base/'review',p)
            c=compare_experiment(base/'review',resamples=100)
            self.assertEqual(c['arms']['reconcile']['micro_f1']['value'],1.0)
            publish(p,base/'run',base/'console',comparison=c)
            published=json.loads((base/'console/comparison.json').read_text())
            self.assertEqual(published['paired'],[])
            self.assertEqual(published['status'],'incomplete')
            self.assertTrue(published['missing'])
            c['paired_by_family']['direction-control']={'a':'reconcile','b':'empty','difference':.5,'ci95':[.2,.8]}
            publish(p,base/'run',base/'direction',comparison=c)
            pair=json.loads((base/'direction/comparison.json').read_text())['paired'][0]
            self.assertEqual((pair['a'],pair['b'],pair['difference']),('empty','reconcile',.5))
            store=ResearchStore([base/'console']);self.assertEqual(store.incompatible,[])
            view=Projection(store);self.assertEqual(len(view.experiment(p['artifact_id'])['attempts']),4)
            self.assertEqual(len(store.artifacts['comparison']),1)
            for a in store.artifacts['attempt']:
                detail=view.attempt(a)
                self.assertIsNotNone(detail['snapshot_id'])
                if detail['variant_id']=='reconcile':self.assertTrue(detail['received'])
            text=''.join(f.read_text() for f in (base/'console').glob('*.json'))
            self.assertNotIn('episode_ref',text)
            self.assertNotIn('truth',text)
            p['episodes'][0]['split']='holdout'
            with self.assertRaises(ValueError):publish(p,base/'run',base/'hidden')

    def test_controller_registry_executes_baselines_and_will_not_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);p=plan();write(base/'plan.json',p)
            cfg={'experiments':{p['artifact_id']:{'plan':str(base/'plan.json'),'output':str(base/'run'),'console':str(base/'console')}}}
            write(base/'registry.json',cfg)
            with mock.patch.dict('os.environ',{'AUDITLAB_RESEARCH_CONTROLLER':str(base/'registry.json')}):
                self.assertTrue(controller.status()['budget_configured'])
                with mock.patch('subprocess.Popen') as launch:
                    controller.start(p['artifact_id']);self.assertEqual(launch.call_count,1)
                controller.worker(p['artifact_id'])
                self.assertEqual(controller.status()['experiments'][p['artifact_id']],'completed')
                with self.assertRaises(controller.ControllerError):controller.start(p['artifact_id'])
                with self.assertRaises(controller.ControllerError):controller.start('../escape')

    def test_timeout_retains_reservation_and_reports_unknown_cost(self):
        class Provider:
            model='priced';is_test_double=False
            def __init__(self):self.calls=0
            def complete(self,request):
                self.calls+=1;raise ProviderError('timeout','connection interrupted')
        provider=Provider();prices={'priced':{'input':100,'output':100}}
        guard=SpendGuard(0.6,prices)
        caller=ModelCaller(provider,RetryPolicy(max_tries=4,base_delay_s=0),guard)
        request=ProviderRequest('hi',[],[],10,1)
        with self.assertRaises(ProviderError):caller.call(request,caller.clock()+10)
        self.assertEqual(provider.calls,1)
        self.assertIsNone(caller.records[0]['cost_usd'])
        self.assertGreater(caller.records[0]['budget_debit_usd'],0)

    def test_symlink_cannot_publish_private_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);private=base/'private';private.mkdir()
            (private/'secret.json').write_text(json.dumps({'schema_version':'1.0','artifact_id':'secret','artifact_kind':'experiment','source_sha':'x','protocol_id':'x'}))
            (base/'experiment.json').symlink_to(private/'secret.json')
            store=ResearchStore([base]);self.assertFalse(store.artifacts['experiment'])
            self.assertEqual(len(store.incompatible),1)

class AllocationMatrixTests(unittest.TestCase):
    def test_observed_full_and_partial_coverage_not_a_hardcoded_pass(self):
        from review.genesis_acceptance.data_checks import SCENARIOS,allocation_coverage
        from review.genesis_acceptance.independent_calc import audit
        from auditlab.generator import generate_episode
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);answers={}
            for did in ('D09','D10','D11'):
                seed,config,_=SCENARIOS[did];ep=base/'episodes'/did
                generate_episode(seed,config,ep);answers[did]=audit(ep/'public')
            self.assertEqual(allocation_coverage(base,answers),{'D09':'omitted','D10':'partial','D11':'partial'})
            answers['D09']['findings']=[]
            self.assertNotIn('omitted',allocation_coverage(base,answers).values())

class SchemaTests(unittest.TestCase):
    def test_public_schema_rejects_unknown_cost_nan_and_private_fields(self):
        from auditlab.research.artifacts import validate
        data={'schema_version':'1.0','artifact_kind':'experiment','artifact_id':'x','source_sha':'x','protocol_id':'x',
              'provenance':{'kind':'measured'},'status':'completed','agents':[],'progress':{}}
        validate(data)
        for patch in ({'cost':float('nan')},{'metadata':{'api_key':'secret'}},{'progress':[]}):
            with self.assertRaises(ValueError):validate({**data,**patch})


class ControllerOriginTests(unittest.TestCase):
    def test_external_site_cannot_start_a_budgeted_local_job(self):
        from fastapi.testclient import TestClient
        from auditlab.demo.server import create_app
        from auditlab.demo.paths import DemoPaths
        with tempfile.TemporaryDirectory() as tmp:
            client=TestClient(create_app(DemoPaths(Path(tmp)),build_samples=False))
            for headers in ({'Origin':'https://unrelated.example'}, {'Sec-Fetch-Site':'cross-site'}, {'Host':'unrelated.example'}):
                response=client.post('/research/experiments/unknown/start',headers=headers)
                self.assertEqual(response.status_code,403)
