# Credence developer targets. See README.md.
SHELL := /bin/bash
CREDENCE_API_BASE_URL ?= http://127.0.0.1:8000
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.21.2
COMPOSE := docker compose -f infra/rippled/docker-compose.yml

.PHONY: setup test test-unit test-conformance test-integration lint typecheck secrets ledger-up ledger-down web

setup:
	git submodule update --init third_party/custody
	uv sync --locked

test: test-unit test-conformance

test-unit:
	uv run pytest tests/unit

test-conformance:
	uv run pytest tests/conformance

# Exit code 5 means "no tests collected", expected until the XRPL workstream adds tests.
test-integration:
	uv run pytest tests/integration; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run python scripts/check_large_files.py

typecheck:
	uv run mypy

secrets:
	docker run --rm -v "$(CURDIR):/repo" $(GITLEAKS_IMAGE) dir /repo --config /repo/.gitleaks.toml --redact --no-banner

ledger-up:
	mkdir -p infra/rippled/data
	$(COMPOSE) up -d

ledger-down:
	$(COMPOSE) down

# Runs the unchanged Lovable frontend with its own package manager (bun) and lockfile.
# The API base URL is exposed as VITE_CREDENCE_API_BASE_URL; the app does not read it yet.
web:
	git submodule update --init web
	@command -v bun >/dev/null || { echo "bun is required for web/ (its lockfile is bun.lock)"; exit 1; }
	cd web && bun install --frozen-lockfile && VITE_CREDENCE_API_BASE_URL=$(CREDENCE_API_BASE_URL) bun run dev
