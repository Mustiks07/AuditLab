# LLM network bridge image

Status: **not built, not checked.** `requirements.in` lists `anthropic` without a pinned
version; `requirements.lock` and the offline wheels in `docker/wheels/bridge/` do not exist
yet, so `docker build` fails at the lock `COPY`. No live Docker run of the topology below
has been performed.

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

## To finish

1. Pin `anthropic` in `requirements.in`, lock it (command in the file header), add wheels.
2. `docker build --network=none -f docker/bridge/Dockerfile -t auditlab-llm-bridge:dev .`
3. `AUDITLAB_BRIDGE_IMAGE=auditlab-llm-bridge:dev python -m unittest discover -s tests/research_agents -p test_bridge_isolation.py -v`
   (the live test checks that the bridge network has no direct route out and that the
   proxy refuses a non-allowlisted target; it never contacts a provider).
