# Remote notebooklm-mcp server — single-tenant, bearer-gated, streamable-HTTP.
#
# The image is stateless: the profile (incl. master_token.json) is mounted at
# runtime, never baked in. Run behind a Cloudflare Tunnel (or any TLS reverse
# proxy) — see deploy/README.md.
FROM python:3.12-slim

# Default to the published package. Override at build time to pin a version or
# install from a local wheel:
#   docker build --build-arg NOTEBOOKLM_SPEC="notebooklm-py[mcp,headless]==X.Y.Z" .
ARG NOTEBOOKLM_SPEC="notebooklm-py[mcp,headless]"

RUN pip install --no-cache-dir "${NOTEBOOKLM_SPEC}"

# Run as a non-root user; the mounted profile dir must be writable by this uid
# (keepalive/recovery rotates cookies into storage_state.json).
RUN useradd --create-home --uid 10001 app
USER app

ENV NOTEBOOKLM_MCP_HOST=0.0.0.0 \
    NOTEBOOKLM_MCP_PORT=8000 \
    NOTEBOOKLM_PROFILE=server

EXPOSE 8000

# Liveness: stdlib-only TCP connect to the bound port. No curl/wget/nc needed,
# and it never touches the auth-gated /mcp route. The port is hardcoded to 8000
# (the image default below) — exec-form HEALTHCHECK can't read env vars, so if
# you override NOTEBOOKLM_MCP_PORT, update this port to match.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',8000))==0 else 1)"]

# --host 0.0.0.0 is non-loopback, so NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1 AND a
# NOTEBOOKLM_MCP_TOKEN are MANDATORY (the entrypoint refuses to start otherwise).
CMD ["notebooklm-mcp", "--transport", "http"]
