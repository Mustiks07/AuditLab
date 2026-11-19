# Policy sandbox image. Contains the policy code and nothing of the trusted side.
# Build from the repo root:  docker build -f docker/runtime/Dockerfile -t auditlab-policy:dev .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Only the policy-facing packages: no evaluator, no fixtures, no private data, no tests.
COPY auditlab/__init__.py auditlab/contracts.py /app/auditlab/
COPY auditlab/policies /app/auditlab/policies
COPY auditlab/runner/__init__.py auditlab/runner/policy_host.py /app/auditlab/runner/
# policy_host.py never imports env.py; the stub __init__ below avoids pulling in trusted modules.
RUN printf '"""Policy sandbox stub."""\n' > /app/auditlab/runner/__init__.py \
 && useradd --uid 65534 --no-create-home --shell /usr/sbin/nologin sandbox 2>/dev/null || true
USER 65534:65534
ENV PYTHONPATH=/app
# Default policy factory; override the arguments to choose another one. Channel = stdin/stdout only.
ENTRYPOINT ["python", "-m", "auditlab.runner.policy_host"]
CMD ["auditlab.policies:make_policy_by_seed", "0"]
