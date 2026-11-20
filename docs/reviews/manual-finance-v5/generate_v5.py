"""Generate the pre-registered v5 matrix with the real generator and seal private/ unread.

usage: python generate_v5.py REPO WORKDIR
WORKDIR/public/<id>/public     the only files used for the answer
WORKDIR/sealed/<id>/private    moved without reading (truth, provenance, source book, transition)
WORKDIR/halves-scan.json       selection log of the pre-registered half rule (public data only)
"""
import json, shutil, sys
from pathlib import Path

repo, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
here = Path(__file__).resolve().parent
sys.path[:0] = [str(repo), str(here)]
from auditlab.generator import generate_episode, GENERATOR_VERSION  # noqa: E402
from scenarios import MATRIX, HALF_FAMILIES, HALF_SEEDS  # noqa: E402
from audit_calc import audit  # noqa: E402


def generate(name, seed, config, keep=True):
    tmp = work / "tmp" / name
    if tmp.exists():
        shutil.rmtree(tmp)
    generate_episode(seed, config, tmp)
    sealed = work / "sealed" / name
    if keep:
        sealed.mkdir(parents=True)
        shutil.move(str(tmp / "private"), str(sealed / "private"))     # moved, never opened
        (work / "public" / name).mkdir(parents=True)
        shutil.move(str(tmp / "public"), str(work / "public" / name / "public"))
    shutil.rmtree(tmp)


print("generator", GENERATOR_VERSION)
for name, (seed, config) in MATRIX.items():
    generate(name, seed, config)
    m = json.loads((work / "public" / name / "public" / "manifest.json").read_text(encoding="utf-8"))
    print(name, seed, m["episode_id"][:10], m["ledger_rows"], m["document_count"])

log = {}
for family, config in HALF_FAMILIES.items():
    want = {"clean": None, "error": None}
    for seed in HALF_SEEDS:
        if all(want.values()):
            break
        probe = work / "probe"
        if probe.exists():
            shutil.rmtree(probe)
        generate_episode(seed, config, probe)
        shutil.rmtree(probe / "private")                                  # deleted unread
        report = audit(probe / "public")
        for line in report["arithmetic"]:
            if "EVEN-FLOOR" not in line:
                continue
            exact_ok = line.split("-> ")[1].split(";")[0].strip() == line.split("recorded ")[1].split(" ")[0]
            kind = "clean" if exact_ok else "error"
            if want[kind] is None:
                want[kind] = {"seed": seed, "line": line}
        shutil.rmtree(probe)
    log[family] = want
    for kind, hit in want.items():
        if hit:
            name = f"H-{family.replace('<-', '-from-')}-{kind}-{hit['seed']}"
            if not (work / "public" / name).exists():
                generate(name, hit["seed"], config)
            print(name, hit["line"][:150])
        else:
            print(f"H-{family}-{kind}: not found in {HALF_SEEDS.start}-{HALF_SEEDS.stop - 1}")
(work / "halves-scan.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
