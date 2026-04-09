# SimpleHarness sandbox image: Python 3.13 + uv + Node 20 + Claude Code CLI.
# The SimpleHarness repo is bind-mounted at /opt/simpleharness at runtime,
# the target worksite at /worksite. See compose.yml.
FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# OS deps. tini becomes PID 1 so SIGINT from `docker compose run` reaches
# the Python harness and drives the Ctrl+C → correction prompt flow.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tini \
    && rm -rf /var/lib/apt/lists/*

# Node 20 LTS so agents can run npm/npx in TypeScript target repos.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv installed system-wide so both root (build) and the harness user see it.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root user. UID 1000 = Docker Desktop's default mapping on Windows hosts.
RUN useradd -m -u 1000 -s /bin/bash harness
USER harness

# Native Claude Code installer. Must run from /tmp — at / it scans the whole
# filesystem and hangs in Docker. Binary lands at ~/.local/bin/claude.
WORKDIR /tmp
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/harness/.local/bin:${PATH}"

WORKDIR /worksite
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/simpleharness/scripts/entrypoint.sh"]
CMD ["simpleharness", "watch"]
