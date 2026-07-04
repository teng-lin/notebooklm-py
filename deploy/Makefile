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

# Pin a published image tag with `make prod VERSION=x.y.z`. Otherwise, keep the
# override order: shell/CLI env, deploy/.env, this checkout's pyproject version,
# then Make's missing-version guard.
_PROJECT_VERSION := $(shell [ -f ../pyproject.toml ] && \
	sed -n '/^\[project\]/,/^\[/ s/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' ../pyproject.toml | tr -d '\r' | head -1)
_VERSION_DOTENV := $(shell [ -f .env ] && sed -n 's/^NOTEBOOKLM_MCP_VERSION=//p' .env | tr -d '\r' | head -1)
ifeq ($(origin VERSION),command line)
ifneq ($(strip $(VERSION)),)
COMPOSE_ENV := NOTEBOOKLM_MCP_VERSION=$(VERSION)
endif
else ifneq ($(origin NOTEBOOKLM_MCP_VERSION),undefined)
COMPOSE_ENV :=
else ifneq ($(_VERSION_DOTENV),)
COMPOSE_ENV :=
else ifneq ($(_PROJECT_VERSION),)
COMPOSE_ENV := NOTEBOOKLM_MCP_VERSION=$(_PROJECT_VERSION)
endif

# Profile-aware compose invocation — selects exactly one tunnel sidecar.
DC = $(COMPOSE_ENV) $(COMPOSE) --profile $(TUNNEL)
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

.PHONY: help setup dev prod up restart logs down config _require-image-version _require-tunnel-token

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

# Friendly pre-check for source-less deploys: without pyproject.toml, the user
# must say which published image tag to pull.
_require-image-version:
	@if [ -z "$(COMPOSE_ENV)" ] && [ -z "$$NOTEBOOKLM_MCP_VERSION" ] \
		&& ! grep -qsE '^NOTEBOOKLM_MCP_VERSION=.+' .env; then \
		echo "✗ NOTEBOOKLM_MCP_VERSION is unknown — run from a source checkout, pass VERSION=x.y.z, or set it in deploy/.env"; \
		exit 1; \
	fi

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

prod: _require-image-version _require-tunnel-token ## Pull a published version and start (VERSION=x.y.z, or pin NOTEBOOKLM_MCP_VERSION in .env)
	$(DC) pull
	$(DC) up -d

dev: _require-image-version _require-tunnel-token ## Build THIS checkout and start (contributors; TUNNEL=cloudflare|tailscale)
	$(DC_BUILD) up -d --build

restart: _require-image-version _require-tunnel-token ## Rebuild THIS checkout + recreate after a source/config change
	$(DC_BUILD) up -d --build

logs: ## Tail the server logs
	$(COMPOSE) logs -f notebooklm-mcp

down: ## Stop and remove the stack (pass the same TUNNEL you started with)
	$(DC) down

config: ## Validate the merged compose config
	$(DC) config
