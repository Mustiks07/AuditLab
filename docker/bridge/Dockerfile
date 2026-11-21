# LLM network bridge image (also runs the stdlib egress proxy). NOT YET BUILT OR CHECKED:
# docker/bridge/requirements.lock does not exist yet, so the COPY below fails closed until the
# integrator pins and locks requirements.in (see docker/bridge/README.md).
# Contains only the public contracts and auditlab/research/agents: no evaluator, runner, generator,
# fixtures, datasets, truth or reports. Build from the repo root:
#   docker build --network=none -f docker/bridge/Dockerfile -t auditlab-llm-bridge:dev .
FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY docker/bridge/requirements.lock /app/requirements.lock
COPY docker/wheels/bridge/ /wheels/
RUN python -m pip install --no-index --find-links=/wheels --no-cache-dir --require-hashes -r /app/requirements.lock
COPY auditlab/__init__.py auditlab/contracts.py /app/auditlab/
COPY auditlab/research/__init__.py /app/auditlab/research/__init__.py
COPY auditlab/research/agents /app/auditlab/research/agents
RUN chmod -R a+rX /app
USER 65534:65534
# Channel = stdin/stdout JSON Lines; first line is the init/config message (no secrets, no paths).
# The provider key arrives only as an environment variable named by `docker create -e NAME`.
ENTRYPOINT ["python", "-m", "auditlab.research.agents.bridge"]
