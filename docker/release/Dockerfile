# Trusted orchestration image. Docker socket belongs to this trusted controller only.
FROM docker:29.0.1-cli@sha256:f7d048590d889e00868920f658e807ecbc4ef662d51c9af67eb5534a447d58d6 AS docker_cli
FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONPATH=/app
WORKDIR /app
COPY requirements.lock /app/requirements.lock
COPY docker/wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels --no-cache-dir --require-hashes -r /app/requirements.lock

# No generator, policies, evaluator, datasets, fixture truth, Docker client or socket.
FROM dependencies AS verifier
COPY auditlab/__init__.py auditlab/contracts.py /app/auditlab/
COPY auditlab/verifier /app/auditlab/verifier
COPY sql/verifier /app/sql/verifier
RUN chmod -R a+rX /app
USER 65534:65534
CMD ["python", "-m", "auditlab.verifier", "/public"]

FROM dependencies AS runtime
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY auditlab /app/auditlab
COPY sql /app/sql
COPY schemas /app/schemas
COPY tests /app/tests
COPY review /app/review
COPY fixtures /app/fixtures
COPY datasets /app/datasets
COPY docs /app/docs
COPY run.py offline.py judge_adapter.py README.md LICENSE .python-version /app/
COPY trajectories.jsonl trajectories.manifest.json /app/
COPY tools /app/tools
COPY docker/release/notices /app/notices
RUN chmod -R a+rX /app
ARG SOURCE_REVISION=uncommitted
LABEL org.opencontainers.image.title="AuditLab trusted offline runtime" org.opencontainers.image.revision=$SOURCE_REVISION
ENV AUDITLAB_SOURCE_REVISION=$SOURCE_REVISION
CMD ["python", "run.py", "verify", "--output", "/results/verify"]
