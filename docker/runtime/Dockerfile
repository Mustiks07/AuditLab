# Policy sandbox image: the policy code and the stdio host only. No evaluator, fixtures, truth or reports.
# Build from the repo root:  docker build -f docker/runtime/Dockerfile -t auditlab-policy:dev .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY auditlab/__init__.py auditlab/contracts.py /app/auditlab/
COPY auditlab/policies /app/auditlab/policies
COPY auditlab/runner/policy_host.py /app/auditlab/runner/policy_host.py
# Empty package marker: the real auditlab/runner/__init__.py imports trusted modules on purpose left out.
RUN printf '' > /app/auditlab/runner/__init__.py
USER 65534:65534
# Channel = stdin/stdout JSON Lines. Arguments: NAME SEED (auditlab.policies.make_policy).
ENTRYPOINT ["python", "-m", "auditlab.runner.policy_host"]
CMD ["random", "0"]
