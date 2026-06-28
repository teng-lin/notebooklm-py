# Convenience wrapper for the remote notebooklm-mcp deploy.
#   make dev    — build + install THIS repo checkout (default)
#   make prod   — build + install a published PyPI release (VERSION=x.y.z)
#   make logs / restart / down / config
# Override the compose command with COMPOSE=... if needed.

COMPOSE ?= docker compose
VERSION ?= 0.8.0

# Run the container as YOUR uid/gid so the mounted profile needs no chown, and
# default the mounted profile to your local "default" profile. All overridable
# (env or `make dev NOTEBOOKLM_PROFILE_DIR=...`); exported so compose sees them.
export NOTEBOOKLM_UID ?= $(shell id -u)
export NOTEBOOKLM_GID ?= $(shell id -g)
export NOTEBOOKLM_PROFILE_DIR ?= $(HOME)/.notebooklm/profiles/default

.PHONY: help dev prod up restart logs down config

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

dev: ## Build from source (this checkout) and start
	$(COMPOSE) up -d --build

prod: ## Build from a published release and start (e.g. make prod VERSION=0.8.0)
	$(COMPOSE) build --build-arg NOTEBOOKLM_SPEC="notebooklm-py[mcp,headless]==$(VERSION)"
	$(COMPOSE) up -d

up: dev ## Alias for `dev`

restart: ## Rebuild + recreate after a source/config change
	$(COMPOSE) up -d --build

logs: ## Tail the server logs
	$(COMPOSE) logs -f notebooklm-mcp

down: ## Stop and remove the stack
	$(COMPOSE) down

config: ## Validate the merged compose config
	$(COMPOSE) config
