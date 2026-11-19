# Policy sandbox image: the policy code and the stdio host only. No evaluator, fixtures, truth or reports.
# Build from the repo root:  docker build -f docker/runtime/Dockerfile -t auditlab-policy:dev .
FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY docker/runtime/requirements.lock /app/requirements.lock
COPY docker/wheels/tzdata-*.whl /wheels/
RUN python -m pip install --no-index --find-links=/wheels --no-cache-dir --require-hashes -r /app/requirements.lock
COPY auditlab/__init__.py auditlab/contracts.py /app/auditlab/
COPY auditlab/policies /app/auditlab/policies
COPY auditlab/runner/policy_host.py /app/auditlab/runner/policy_host.py
# Empty package marker: the real auditlab/runner/__init__.py imports trusted modules on purpose left out.
RUN printf '' > /app/auditlab/runner/__init__.py
RUN chmod -R a+rX /app
USER 65534:65534
# Channel = stdin/stdout JSON Lines. Arguments: NAME SEED (auditlab.policies.make_policy).
ENTRYPOINT ["python", "-m", "auditlab.runner.policy_host"]
CMD ["random", "0"]
