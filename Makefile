VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt typecheck check context propose show commit serve docker clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create .venv and install the package with dev extras
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e '.[dev]'
	@test -f .env || (cp .env.example .env && echo "Created .env -- fill it in.")

test: ## Run the test suite (no network required)
	$(PY) -m pytest -q

lint: ## Ruff
	$(VENV)/bin/ruff check src tests

fmt: ## Ruff autofix + format
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

typecheck: ## mypy over the package
	$(VENV)/bin/mypy src/fpl_buddy

check: lint test ## Lint and test -- run this before you commit

context: ## Print the brief the agent would see
	$(VENV)/bin/fpl-buddy context

propose: ## Run the agent now and store a proposal
	$(VENV)/bin/fpl-buddy propose

show: ## Show the latest proposal
	$(VENV)/bin/fpl-buddy show

commit: ## Run the deadline job now (respects DRY_RUN)
	$(VENV)/bin/fpl-buddy commit

serve: ## Run the API and scheduler locally
	$(VENV)/bin/fpl-buddy serve

docker: ## Build the container image
	docker build -t fpl-buddy:local .

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
