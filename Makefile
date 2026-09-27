# Annona — developer and operator entry points.
#
# Everything here works offline. `make demo` runs a real agentic loop with real
# tool execution and no credentials, which is the fastest way to see what this
# repository actually does; `make verify` is the one an operator runs on a new
# appliance before handing it over.

PY  := env/bin/python
PIP := env/bin/pip

.DEFAULT_GOAL := help
.PHONY: help setup hooks test test-cov test-live test-container lint format typecheck contracts \
        check demo run verify image image-multiarch up down docs docs-serve clean paper

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install runtime + dev dependencies
	@test -d env || python3 -m venv env
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements-dev.txt
	$(PIP) install --quiet -e .
	@$(MAKE) --no-print-directory hooks
	@echo "ready — try 'make demo' or 'make check'"

hooks: ## Refuse pushes that are not green (runs `make check` before every push)
	@git rev-parse --git-dir >/dev/null 2>&1 && git config core.hooksPath .githooks \
		&& echo "pre-push hook on: every push runs make check" || true

test: ## Run the test suite
	$(PY) -m pytest

test-cov: ## Run the test suite with a coverage report
	$(PY) -m pytest --cov=runner --cov-report=term-missing --cov-report=html

test-live: ## Run the tests that need a real local model (Ollama must be up)
	ANNONA_LIVE_OLLAMA=1 $(PY) -m pytest -m live -v

test-container: image ## Run the tests that need a Docker daemon
	ANNONA_CONTAINER_TESTS=1 $(PY) -m pytest -m container -v

lint: ## Lint (ruff): rules and formatting, exactly as CI checks them
	$(PY) -m ruff check runner tests
	$(PY) -m ruff format --check runner tests

format: ## Format (ruff)
	$(PY) -m ruff format runner tests
	$(PY) -m ruff check --fix runner tests

typecheck: ## Static types (mypy)
	$(PY) -m mypy

contracts: ## Verify the architectural import contracts
	env/bin/lint-imports

check: lint typecheck contracts test ## Everything CI runs

demo: ## Offline end-to-end agentic run: no credentials, no network
	$(PY) -m runner.demo

run: ## Start the daemon and local UI on 127.0.0.1:7070
	./start.sh

verify: ## Acceptance run against a local model: placement, leak rate, ledger
	$(PY) deploy/verify_appliance.py --model $${ANNONA_LIVE_MODEL:-qwen2.5:14b}

image: ## Build the container image for this machine's architecture
	docker build -t annona:dev .

image-multiarch: ## Build for arm64 (DGX Spark) and amd64, the release matrix
	docker buildx build --platform linux/arm64,linux/amd64 -t annona:dev .

up: ## Start the appliance: kernel + local model, on this machine
	docker compose up -d

down: ## Stop the appliance, keeping volumes (policy, ledger, vault)
	docker compose down

paper: ## The paper → paper/build/main.pdf (tectonic)
	mkdir -p paper/build && cd paper && tectonic -X compile main.tex --outdir build

docs: ## Build the documentation site
	$(PY) -m mkdocs build --strict

docs-serve: ## Serve the documentation site with live reload
	$(PY) -m mkdocs serve

clean: ## Remove build and test artefacts
	rm -rf build dist htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache site
	find . -type d -name __pycache__ -not -path "./env/*" -exec rm -rf {} +
