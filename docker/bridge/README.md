# LLM network bridge image

The integrator supplies `anthropic==2.12.0` and a hash-locked transitive dependency set.
Build and the internal-network/proxy refusal check are exercised in PR CI. This checks
network isolation, not a live provider response. Wheels are downloaded into the separate
`docker/wheels/bridge/` directory with `--require-hashes`; no SDK is added to the offline core.

## Topology

```
controller ──stdin/stdout JSONL──▶ bridge container          network: <name> (--internal, no gateway)
                                     │ HTTPS_PROXY=http://<name>-proxy:3128
                                     ▼
                                   egress proxy container      networks: <name> + <name>-egress
                                     │ CONNECT only to allowlisted host:port (default api.anthropic.com:443)
                                     ▼
                                   provider API (TLS end to end; the proxy never decrypts)
```

`auditlab.research.agents.sandbox.docker_bridge_commands()` returns the exact argv lists;
`DockerBridgeProcess` runs them and tears everything down on close.

* Bridge: `--pull=never --read-only --cap-drop ALL --security-opt no-new-privileges
  --user 65534:65534 --tmpfs /tmp:rw,noexec,nosuid,size=64m --pids-limit`, CPU/memory
  limits, `--log-driver none`, **no volume mounts**. Config is the first stdin line.
  The key is passed as `-e NAME`, so Docker copies the value from the controller's
  environment; it never appears in argv. It is still visible to anyone who can run
  `docker inspect` on the host while the container exists.
* Proxy: the same image with `--entrypoint python -m auditlab.research.agents.egress_proxy`,
  same hardening, no secret. It logs one JSON line per decision (no headers/bodies) to
  `docker logs <name>-proxy`; it rejects host names that resolve to non-public addresses.
* The image copies only `auditlab/__init__.py`, `auditlab/contracts.py`,
  `auditlab/research/__init__.py` and `auditlab/research/agents/`.

`dev_bridge_command()` is a host subprocess with **no network or filesystem isolation**; it
is refused for the official protocol, like the runner's `--unsafe-dev-subprocess`.

## Reproduce

1. `python -m pip download --require-hashes --only-binary=:all: --dest docker/wheels/bridge -r docker/bridge/requirements.lock`
2. `docker build --network=none -f docker/bridge/Dockerfile -t auditlab-llm-bridge:dev .`
3. `AUDITLAB_BRIDGE_IMAGE=auditlab-llm-bridge:dev python -m unittest discover -s tests/research_agents -p test_bridge_isolation.py -v`
   (the live test checks that the bridge network has no direct route out and that the
   proxy refuses a non-allowlisted target; it never contacts a provider).
