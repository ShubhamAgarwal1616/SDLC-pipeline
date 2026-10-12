# Phase 2 agent sandbox: Claude Code headless + OpenSpec + the org skill library.
# No secrets are baked in. At runtime the container sits on a network that only reaches llm-gateway
# (agent) or no network at all (verify / tool containers). There's no curl or wget in the final image.

# --- build stage: fetch static binaries (the only stage with a download tool) ---
FROM debian:bookworm-slim AS fetch
ARG GITLEAKS_VERSION=8.30.1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN arch="$(dpkg --print-architecture)" && case "$arch" in arm64) ga=arm64 ;; amd64) ga=x64 ;; *) echo "unsupported $arch"; exit 1 ;; esac \
 && curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${ga}.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks

# --- final image ---
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.296
ARG OPENSPEC_VERSION=1.14.1
ARG SEMGREP_VERSION=1.180.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-venv ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@fission-ai/openspec@${OPENSPEC_VERSION}" \
 && npm cache clean --force
# semgrep runs offline in verify with the local rules below (no registry, no metrics).
RUN python3 -m venv /opt/semgrep && /opt/semgrep/bin/pip install --no-cache-dir "semgrep==${SEMGREP_VERSION}" \
 && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep

# JDK 21 for Kotlin/Gradle projects (P5), and gitleaks for the verify runner's secret scan.
COPY --from=eclipse-temurin:21-jdk /opt/java/openjdk /opt/java/openjdk
COPY --from=fetch /usr/local/bin/gitleaks /usr/local/bin/gitleaks

RUN useradd -m -u 10001 worker && mkdir -p /run/claude /run/gradle /workspace /io && chown worker /run/claude /run/gradle /workspace /io

# Org policy: managed settings win over anything a repo's .claude/settings.json says (PLAN.md §24).
COPY worker/managed-settings.json /etc/claude-code/managed-settings.json
COPY worker/managed-mcp.json /etc/claude-code/managed-mcp.json
COPY worker/semgrep-rules/ /opt/sdlc/semgrep-rules/

# Template for CLAUDE_CONFIG_DIR: org CLAUDE.md + the org skill library. Re-synced into each run volume.
COPY worker/claude-home/ /opt/sdlc/claude-home/
COPY skills/ /opt/sdlc/claude-home/skills/
COPY worker/entrypoint.py /opt/sdlc/entrypoint.py

ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH \
    GRADLE_USER_HOME=/run/gradle \
    GIT_AUTHOR_NAME=sdlc-agent GIT_AUTHOR_EMAIL=sdlc-agent@users.noreply.github.com \
    GIT_COMMITTER_NAME=sdlc-agent GIT_COMMITTER_EMAIL=sdlc-agent@users.noreply.github.com \
    CLAUDE_CONFIG_DIR=/run/claude \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    OPENSPEC_TELEMETRY=0 \
    SEMGREP_SEND_METRICS=off \
    DO_NOT_TRACK=1

USER worker
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/sdlc/entrypoint.py"]
