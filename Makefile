.DEFAULT_GOAL := help
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Create venv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt

test: ## Run tests
	$(PYTHON) -m pytest tests/

lint: ## Run linter
	$(PYTHON) -m ruff check agents/ tools/ tests/ relay/

download-model: ## Pre-download the semantic search embedding model (~90MB)
	$(PYTHON) -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2'); print('Model cached.')"

bootstrap: download-model ## Generate settings files from config templates
	$(PYTHON) -m tools.bootstrap

verify: ## Run onboarding tests to verify setup
	$(PYTHON) -m pytest tests/test_onboarding.py -q

relay-build: ## Build relay container (run on relay machine)
	podman build -t dci-relay -f container/Containerfile.relay .

relay-start: ## Start relay container (run on relay machine)
	bash container/relay.sh start

relay-stop: ## Stop relay container
	bash container/relay.sh stop

clean: ## Remove build artifacts and caches
	rm -rf __pycache__ agents/__pycache__ relay/__pycache__ tools/__pycache__ tests/__pycache__
	rm -rf .pytest_cache .ruff_cache
	rm -rf *.egg-info dist build

.PHONY: help install test lint download-model bootstrap verify relay-build relay-start relay-stop clean
