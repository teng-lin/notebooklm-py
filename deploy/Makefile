# Convenience wrapper for the remote notebooklm-mcp deploy.
#   make setup                  — interactive first run: pick a tunnel + generate secrets → .env
#   make up                     — pull the published image and start (the easy path)
#   make prod VERSION=x.y.z      — pull a specific published version and start
#   make dev                    — build THIS checkout + start (contributors)
#   make dev TUNNEL=tailscale   — same, but with the Tailscale Funnel sidecar (no domain)
#   make logs / restart / down / config
# TUNNEL selects the tunnel sidecar (Compose profile): cloudflare (default) or tailscale.
# Override the compose command with COMPOSE=... if needed.

COMPOSE ?= docker compose
# TUNNEL: a CLI/env override wins; else the choice `make setup` saved in .env;
# else cloudflare. Only TUNNEL is read from .env into make — it's make-only (not a
# compose var), so it can't collide with compose's own reading of .env.
_TUNNEL_DOTENV := $(shell [ -f .env ] && sed -n 's/^TUNNEL=//p' .env | tr -d '\r' | head -1)
TUNNEL ?= $(if $(_TUNNEL_DOTENV),$(_TUNNEL_DOTENV),cloudflare)

# Pin a published image tag with `make prod VERSION=x.y.z`. Export it to compose
# ONLY when given on the command line; otherwise leave it unset so compose reads
# NOTEBOOKLM_MCP_VERSION from .env (default 0.8.0). Same reasoning as the
# NOTEBOOKLM_PROFILE_DIR note below: a make-exported default would OUTRANK the .env
# value (shell env > .env) and silently ignore a .env pin.
ifeq ($(origin VERSION),command line)
VERSION_ENV := NOTEBOOKLM_MCP_VERSION=$(VERSION)
endif

# Profile-aware compose invocation — selects exactly one tunnel sidecar.
DC = $(COMPOSE) --profile $(TUNNEL)
# Same, but layer the build override so the image is built from THIS checkout.
DC_BUILD = $(DC) -f docker-compose.yml -f docker-compose.build.yml

# Run the container as YOUR uid/gid so the mounted profile needs no chown.
export NOTEBOOKLM_UID ?= $(shell id -u)
export NOTEBOOKLM_GID ?= $(shell id -g)
# NOTE: do NOT set/export NOTEBOOKLM_PROFILE_DIR here. make does not read .env, so a
# `?=` default would resolve to "default" and `export` would push it into compose's
# env, where it OUTRANKS the .env value (shell env > .env) — silently ignoring the
# user's `.env` setting. Compose reads NOTEBOOKLM_PROFILE_DIR from .env directly and
# defaults to ~/.notebooklm/profiles/default on its own; a shell/CLI override such as
# `NOTEBOOKLM_PROFILE_DIR=… make dev` is still auto-exported by make to the recipe.

.PHONY: help setup dev prod up restart logs down config _require-tunnel-token

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

# Friendly pre-check for the chosen tunnel's token (shell env OR deploy/.env). Compose
# can't do a profile-scoped required-var (it interpolates the whole file before profile
# filtering), so the guard lives here instead of a `:?` in docker-compose.yml.
_require-tunnel-token:
	@if [ "$(TUNNEL)" = "cloudflare" ]; then \
		{ [ -n "$$CF_TUNNEL_TOKEN" ] || grep -qsE '^CF_TUNNEL_TOKEN=.+' .env; } \
			|| { echo "✗ TUNNEL=cloudflare needs CF_TUNNEL_TOKEN — set it in deploy/.env"; exit 1; }; \
	elif [ "$(TUNNEL)" = "tailscale" ]; then \
		{ [ -n "$$TS_AUTHKEY" ] || grep -qsE '^TS_AUTHKEY=.+' .env; } \
			|| { echo "✗ TUNNEL=tailscale needs TS_AUTHKEY — set it in deploy/.env"; exit 1; }; \
	else echo "✗ Unknown TUNNEL=$(TUNNEL) (use cloudflare or tailscale)"; exit 1; fi

setup: ## Interactive first run: pick a tunnel + generate secrets → writes .env
	@sh setup.sh

up: prod ## Pull the published image and start (the easy path)

prod: _require-tunnel-token ## Pull a published version and start (VERSION=x.y.z, or pin NOTEBOOKLM_MCP_VERSION in .env)
	$(VERSION_ENV) $(DC) pull
	$(VERSION_ENV) $(DC) up -d

dev: _require-tunnel-token ## Build THIS checkout and start (contributors; TUNNEL=cloudflare|tailscale)
	$(DC_BUILD) up -d --build

restart: ## Rebuild THIS checkout + recreate after a source/config change
	$(DC_BUILD) up -d --build

logs: ## Tail the server logs
	$(COMPOSE) logs -f notebooklm-mcp

down: ## Stop and remove the stack (pass the same TUNNEL you started with)
	$(DC) down

config: ## Validate the merged compose config
	$(DC) config
