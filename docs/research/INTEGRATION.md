# Integrated research workflow

The optional research profile connects the generator, bounded real investigations,
independent financial validation, repeated failure reduction, curriculum preparation,
SFT export and the C3 console. Local policies are identified as such. An SFT file is
not a trained model. API measurements, weight updates and the sealed final holdout
remain `not_checked` until executed with available access and resources.

From a clean, non-shallow checkout, Python 3.11.14:

```sh
python -m pip install --require-hashes -r requirements.lock
python tools/research_rehearsal.py --output artifacts/research-rehearsal
python -m auditlab.demo --root artifacts/research-console serve --port 8765 \
  --research artifacts/research-rehearsal/console-search \
  --research artifacts/research-rehearsal/console-baselines
```

Open `http://127.0.0.1:8765/#/research`. The rehearsal creates temporary detached
worktrees for the pinned independent validator and teacher, then removes them.
It performs no external inference or weight training. The baseline comparison uses
published dev episodes; these are not a sealed generalization test. The published
curriculum plan contains a public partition fixture named test; that is not the
private final holdout, and it never enters search, training or the console export.

## Commands and boundaries

`python -m auditlab.research` provides `validate-plan`, `plan`, `run`, `search`,
`build-curriculum`, `train`, `compare`, `replay`, `publish`, `publish-curriculum`,
`preflight`, `smoke`, `summarize`, `export-review`. Each operation has `--help`.
`search` runs the frozen search, repeated reduction and curriculum export together.
`replay` independently recomputes the saved answers; it does not call a model again.
Native execution artifacts remain on the trusted side. `publish` and
`publish-curriculum` explicitly convert them to the public console envelopes in
`schemas/research/*-1.0.json`; they do not publish raw provider configuration,
private snapshots, holdout answers or inner model reasoning.

For server-controlled runs set `AUDITLAB_RESEARCH_CONTROLLER` to a trusted JSON file:

```json
{"experiments":{"my-experiment":{
  "plan":"/absolute/path/plan.json",
  "output":"/absolute/path/run",
  "console":"/absolute/path/public-console",
  "allow_provider_calls":false
}}}
```

The plan's artifact_id must match the registered id. The browser supplies only that
id, never a command, key, path, model or budget. With baseline policies no provider
key is required. API jobs additionally require a configured key, explicit positive
server budgets and `allow_provider_calls: true`. Use Docker transport for API runs.
Stop finishes the bounded current attempt and starts no further attempts; the UI
shows `stopping` until completion. A persistent job lock prevents double-clicks and
relaunch after server restart. CLI resume requires reviewing saved state; changed
plans require a fresh output directory. Never remove a lock while a worker runs.

Provider errors with unknown usage retain a conservative reservation rather than
claiming zero cost. Interrupted paid attempts reserve their full attempt budget.
Reports distinguish known usage cost from the budget debit; unknown costs stay null.
The price table must be checked for the selected provider/model before real use.

## Verification

```sh
python -m pip download --require-hashes --only-binary=:all: \
  --dest docker/wheels/bridge -r docker/bridge/requirements.lock
docker build --network=none -f docker/bridge/Dockerfile -t auditlab-llm-bridge:review .
AUDITLAB_BRIDGE_IMAGE=auditlab-llm-bridge:review \
AUDITLAB_POLICY_IMAGE=YOUR_LOADED_POLICY_IMAGE \
python run.py verify --include-research --timeout 300 --output artifacts/research-checks
```

CI performs the same bridge build and requires every research test to execute,
including the real internal-network/proxy topology check. The ordinary offline
rehearsal retains the original core suite and does not need an API bridge or SDK.
An unconfigured provider and a missing GPU are explicit unmeasured conditions,
not passing experiments. Historical RC3 evidence is unchanged.
