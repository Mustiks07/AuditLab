"""Ordinary reconcile via the real runner (public actions only), plus the verifier as a separate cross-check."""
import copy, json, sys, shutil, tempfile
from pathlib import Path
repo = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(repo))
from auditlab.runner import make_env
from auditlab.policies import make_policy
from auditlab.verifier import verify_report
from auditlab.evaluator import evaluate
out = Path(tempfile.mkdtemp(prefix="v3runs-"))
for ep in sorted(Path("episodes").iterdir()):
    env = make_env("train"); run = out / ep.name
    obs, _ = env.reset(23, {"snapshot_dir": str(ep.resolve()), "export_dir": str(run), "policy_name": "reconcile"})
    pol = make_policy("reconcile", 23); steps = 0
    while True:
        obs, reward, term, trunc, info = env.step(pol.act(copy.deepcopy(obs))); steps += 1
        if term or trunc: break
    score = json.loads((run / "score.json").read_text(encoding="utf-8"))
    pub = out / f"{ep.name}-pub" / "public"; shutil.copytree(ep / "public", pub)
    rep = verify_report(pub)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    v = evaluate({"episode_id": truth["episode_id"], "findings": rep["findings"]}, truth, 0).score
    print(f"{ep.name:22} reconcile steps={steps} TP/FP/FN={score['tp']}/{score['fp']}/{score['fn']} reward={score['reward']:.3f} truncated={trunc} | verifier {rep['engine']} TP/FP/FN={v['tp']}/{v['fp']}/{v['fn']} notes={len(rep['notes'])}")
shutil.rmtree(out, ignore_errors=True)
