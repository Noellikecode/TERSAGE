# FIRST DUE task runner.
#
# Four commands the project promises:
#   make demo            credential-free demo (backend + console)
#   make verify          the complete verification suite
#   make reset           deterministic demo reset
#   make deploy-staging  documented staging deployment
#   make infra-check     Terraform validation, no credentials needed

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV      ?= uv
TOFU    ?= tofu
NPM     ?= npm
FRONT   := frontend
API_PORT   ?= 8000
FRONT_PORT ?= 3000
# Local emulator endpoints. `make up` starts them; `make test-emulator` uses them.
FIRESTORE_EMULATOR_HOST ?= 127.0.0.1:8081
PUBSUB_EMULATOR_HOST    ?= 127.0.0.1:8085

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ setup ---

.PHONY: setup
setup: setup-backend setup-frontend ## Install both toolchains

.PHONY: setup-backend
setup-backend: ## Install Python 3.12 and backend dependencies
	$(UV) python install 3.12
	$(UV) sync

.PHONY: setup-frontend
setup-frontend: ## Install console dependencies
	cd $(FRONT) && $(NPM) install --no-audit --no-fund

# ------------------------------------------------------------------- demo ---

.PHONY: demo
demo: seed ## Start the credential-free demo (API + console)
	@echo "FIRST DUE - fake mode, no credentials required"
	@echo "  api     http://localhost:$(API_PORT)"
	@echo "  console http://localhost:$(FRONT_PORT)"
	@echo "  console token derived from DEMO_SEED; see \`firstdue status\`"
	@trap 'kill 0' EXIT INT TERM; \
	 USE_FAKE_AGENTS=true PORT=$(API_PORT) $(UV) run firstdue serve & \
	 cd $(FRONT) && \
	   FIRSTDUE_CONSOLE_TOKEN=$$($(UV) run --directory .. firstdue status | awk '/^  chief/{print $$2}') \
	   NEXT_PUBLIC_API_BASE_URL=http://localhost:$(API_PORT) $(NPM) run dev -- -p $(FRONT_PORT) & \
	 wait

.PHONY: serve
serve: ## Start only the API
	USE_FAKE_AGENTS=true PORT=$(API_PORT) $(UV) run firstdue serve --reload

.PHONY: console
console: ## Start only the console
	cd $(FRONT) && $(NPM) run dev -- -p $(FRONT_PORT)

# -------------------------------------------------------------- demo state ---

.PHONY: slow-loop
slow-loop: ## Run one complete slow-loop pass (no credentials)
	USE_FAKE_AGENTS=true $(UV) run firstdue slow-loop

.PHONY: seed
seed: ## Build deterministic demo state
	$(UV) run firstdue seed

.PHONY: reset
reset: ## Clear and rebuild demo state deterministically
	$(UV) run firstdue reset
	$(UV) run firstdue verify-seed

# ------------------------------------------------------------- verification ---

.PHONY: verify
verify: lint typecheck test schema verify-seed frontend-verify secret-scan ## Run everything
	@echo ""
	@echo "verification complete"

.PHONY: lint
lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Apply formatting
	$(UV) run ruff check . --fix
	$(UV) run ruff format .

.PHONY: typecheck
typecheck: ## Strict mypy
	$(UV) run mypy

.PHONY: test
test: ## Backend tests (Firestore/Pub/Sub contract tests skip without emulators)
	$(UV) run pytest

.PHONY: test-emulator
test-emulator: ## Contract tests against the Firestore and Pub/Sub emulators
	FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) \
	PUBSUB_EMULATOR_HOST=$(PUBSUB_EMULATOR_HOST) \
	$(UV) run pytest tests/contract -v

.PHONY: test-cov
test-cov: ## Backend tests with coverage
	$(UV) run pytest --cov --cov-report=term-missing

.PHONY: schema
schema: ## Generate the OpenAPI document
	$(UV) run firstdue schema --out docs/openapi.json

.PHONY: verify-seed
verify-seed: seed ## Prove the demo seed is deterministic
	$(UV) run firstdue verify-seed

.PHONY: frontend-verify
frontend-verify: ## Console lint, types, tests, build
	cd $(FRONT) && $(NPM) run lint
	cd $(FRONT) && $(NPM) run typecheck
	cd $(FRONT) && $(NPM) run test
	cd $(FRONT) && $(NPM) run build

.PHONY: secret-scan
secret-scan: ## Scan the full history for credentials (same command CI runs)
	@if command -v gitleaks >/dev/null 2>&1; then \
	  gitleaks detect --source . --config .gitleaks.toml --redact --no-banner --exit-code 1 ; \
	else \
	  echo "gitleaks not installed locally; CI runs it on every push."; \
	  echo "install: brew install gitleaks"; \
	fi

# ---------------------------------------------------------------- containers ---

.PHONY: up
up: ## Start local dependencies (Firestore + Pub/Sub emulators)
	docker compose up -d firestore pubsub

.PHONY: down
down: ## Stop local dependencies
	docker compose down -v

.PHONY: docker-build
docker-build: ## Build both container images
	docker build -f backend/Dockerfile -t firstdue-backend:local .
	docker build -f frontend/Dockerfile -t firstdue-console:local .

.PHONY: docker-smoke
docker-smoke: docker-build ## Build images and prove they serve on $$PORT as non-root
	docker run --rm -d --name firstdue-smoke -e PORT=8080 -p 8080:8080 firstdue-backend:local
	@sleep 3
	@echo "user: $$(docker exec firstdue-smoke id -un)"
	curl -fsS http://localhost:8080/healthz && echo ""
	docker stop firstdue-smoke

# ---------------------------------------------------------------- deployment ---

.PHONY: bootstrap-infra
bootstrap-infra: ## One-time: create the Terraform state bucket
	./infra/bootstrap.sh

.PHONY: infra-check
infra-check: ## Terraform formatting and validation, no credentials needed
	$(TOFU) fmt -check -recursive infra/terraform
	$(TOFU) -chdir=infra/terraform/envs/staging init -backend=false -input=false >/dev/null
	$(TOFU) -chdir=infra/terraform/envs/staging validate
	$(TOFU) -chdir=infra/terraform/envs/prod init -backend=false -input=false >/dev/null
	$(TOFU) -chdir=infra/terraform/envs/prod validate
	$(UV) run pytest tests/infra -q

.PHONY: infra-plan
infra-plan: ## Plan staging without applying. Read this before deploying.
	$(TOFU) -chdir=infra/terraform/envs/staging init -backend-config=backend.hcl -input=false
	$(TOFU) -chdir=infra/terraform/envs/staging plan -input=false

.PHONY: deploy-staging
deploy-staging: ## Deploy to staging (see docs/deploy.md)
	./infra/deploy-staging.sh

.PHONY: smoke-staging
smoke-staging: ## Six checks against a deployed environment. Needs STAGING_BASE_URL.
	@[ -n "$$STAGING_BASE_URL" ] || { echo "STAGING_BASE_URL is required"; exit 1; }
	$(UV) run pytest tests/staging -v

.PHONY: clean
clean: ## Remove build and state artefacts
	rm -rf .demo-state .pytest_cache .ruff_cache .mypy_cache
	rm -rf $(FRONT)/.next $(FRONT)/node_modules/.cache
