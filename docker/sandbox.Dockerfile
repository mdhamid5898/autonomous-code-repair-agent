# Mechanic — agent execution sandbox.
# A throwaway environment where the agent's bash commands and pytest run,
# isolated from the host. The target repo is bind-mounted at /repo at runtime
# and installed editable there (not baked in), so one image serves every repo.
#
# Build (small context — this dir only):
#   docker build -t mechanic-sandbox -f docker/sandbox.Dockerfile docker/
#
# NOTE: network is currently ON so `pip install -e .` can reach PyPI. Locking it
# down (--network none + pre-provisioned deps) is a later hardening sub-step.
FROM python:3.11-slim

# git: some repos use setuptools_scm (needs `git describe`) during editable install.
# pytest: the deterministic guardrail runs inside here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest

WORKDIR /repo
