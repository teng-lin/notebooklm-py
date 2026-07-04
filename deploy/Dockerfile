# Remote notebooklm-mcp server — single-tenant, bearer-gated, streamable-HTTP.
#
# The image is stateless: the profile (incl. master_token.json) is mounted at
# runtime, never baked in. Run behind a Cloudflare Tunnel (or any TLS reverse
# proxy) — see deploy/README.md.
#
# Two install modes (the compose build context is the repo root):
#   * DEV / from-source (DEFAULT): installs THIS checkout, so `docker compose up`
#     deploys exactly the code in this repo. Leave NOTEBOOKLM_SPEC empty.
#   * PRODUCTION / from PyPI: build with a release spec, e.g.
#       docker compose build --build-arg NOTEBOOKLM_SPEC="notebooklm-py[mcp,headless]==<version>"
#     (or set it under build.args in docker-compose.yml).
FROM python:3.12-slim

ARG NOTEBOOKLM_SPEC=""

# Copy the repo source (build context = repo root; see .dockerignore for excludes).
COPY . /src

# Empty NOTEBOOKLM_SPEC → install the copied source (dev). Non-empty → install
# that spec from PyPI (production) and ignore the copied source.
RUN if [ -n "$NOTEBOOKLM_SPEC" ]; then \
        pip install --no-cache-dir "$NOTEBOOKLM_SPEC"; \
    else \
        pip install --no-cache-dir "/src[mcp,headless]"; \
    fi

# Run as a non-root user; the mounted profile dir must be writable by this uid
# (keepalive/recovery rotates cookies into storage_state.json).
RUN useradd --create-home --uid 10001 app
USER app

ENV NOTEBOOKLM_MCP_HOST=0.0.0.0 \
    NOTEBOOKLM_MCP_PORT=9420 \
    NOTEBOOKLM_PROFILE=server

EXPOSE 9420

# Liveness: stdlib-only TCP connect to the listener over loopback. No curl/wget/nc
# needed, and it never touches the auth-gated /mcp route. The python process reads
# NOTEBOOKLM_MCP_PORT from its own env at runtime (a program sees its environment
# even in exec-form — only shell `$VAR` substitution doesn't), so overriding the
# port keeps the healthcheck in sync. Host is always loopback: the server binds
# 0.0.0.0, which you connect to via 127.0.0.1 from inside the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,socket,sys; p=int(os.environ.get('NOTEBOOKLM_MCP_PORT','9420')); s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',p))==0 else 1)"]

# --host 0.0.0.0 is non-loopback, so NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1 AND a
# NOTEBOOKLM_MCP_TOKEN are MANDATORY (the entrypoint refuses to start otherwise).
CMD ["notebooklm-mcp", "--transport", "http"]
