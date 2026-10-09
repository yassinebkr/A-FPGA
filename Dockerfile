# syntax=docker/dockerfile:1
# Backend + EDA image. Base is SiliconCompiler's own runner image: tools and
# SC cannot drift apart (they are built from SC's _tools.json). Pin tag AND
# digest; bump deliberately and smoke-test on every bump (SC issue #5241).
# Digest resolved 2026-09-19 via the GHCR manifests API.
FROM ghcr.io/siliconcompiler/sc_runner:v0.38.8@sha256:42111016dd7f68cdf20075c37dda4cffc70d066b90683cf6f0284901136b143b AS runtime

# --- Tooling the minimal base may lack (patch for the diff below) ---
RUN apt-get update && apt-get install -y --no-install-recommends patch \
 && rm -rf /var/lib/apt/lists/*

# --- App deps into the same Python env that already carries siliconcompiler ---
# (sc_runner has SC installed in its system python; do NOT create an isolated
#  venv without --system-site-packages or `import siliconcompiler` breaks.)
# requirements.lock is generated from requirements.txt WITH HASHES:
#   uv pip compile backend/requirements.txt -c <pin siliconcompiler==0.38.8> \
#       --generate-hashes -o backend/requirements.lock
# Ubuntu-24 pythons are PEP-668 "externally managed" → the pip fallback passes
# --break-system-packages (intentional, harmless in this single-purpose image).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY backend/requirements.lock /tmp/requirements.lock
RUN uv pip install --system --no-cache -r /tmp/requirements.lock \
 || pip install --no-cache-dir --break-system-packages -r /tmp/requirements.lock

# --- The _apr.py utilization patch, carried as a checked-in unified diff ---
# Fails loudly (non-zero exit) if upstream drifts — that is the point.
COPY patches/apr-utilization-guard.patch /opt/patches/apr-utilization-guard.patch
RUN cd "$(python -c 'import siliconcompiler,pathlib;print(pathlib.Path(siliconcompiler.__file__).parent)')" \
 && patch -p1 --dry-run < /opt/patches/apr-utilization-guard.patch \
 && patch -p1          < /opt/patches/apr-utilization-guard.patch

# --- App ---
# agent gets uid/gid 1000 so bind-mounted host dirs (projects/, logs/) stay
# writable; Ubuntu 24 base images ship a default 'ubuntu' user with uid 1000.
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && groupadd -g 1000 agent && useradd -m -u 1000 -g agent agent \
 && mkdir -p /data /home/agent/.sc && chown -R agent:agent /data /home/agent/.sc
COPY backend/ /app/backend/
RUN mkdir -p /app/backend/logs /app/backend/projects \
 && chown -R agent:agent /app/backend
WORKDIR /app/backend
ENV SCPATH=/data/siliconcompiler \
    SC_BUILDS=/data/sc_builds \
    PYTHONUNBUFFERED=1
USER agent
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')" || exit 1
# No --reload in the container. Graceful shutdown flag stays mandatory (see AGENTS.md).
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "3"]
