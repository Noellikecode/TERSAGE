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
DISTRICT ?= sffd-district-03
API_PORT   ?= 8000
FRONT_PORT ?= 3000

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
	 USE_FAKE_AGENTS=true DEMO_PRIME_SLOW_LOOP=true PORT=$(API_PORT) $(UV) run firstdue serve & \
	 cd $(FRONT) && \
	   FIRSTDUE_CONSOLE_TOKEN=$$($(UV) run --directory .. firstdue status | awk '/^  chief/{print $$2}') \
	   NEXT_PUBLIC_API_BASE_URL=http://localhost:$(API_PORT) $(NPM) run dev -- -p $(FRONT_PORT) & \
	 wait

.PHONY: serve
serve: ## Start only the API
	USE_FAKE_AGENTS=true DEMO_PRIME_SLOW_LOOP=true PORT=$(API_PORT) $(UV) run firstdue serve --reload

.PHONY: console
console: ## Start only the console
	cd $(FRONT) && $(NPM) run dev -- -p $(FRONT_PORT)

# -------------------------------------------------------------- demo state ---

.PHONY: slow-loop
slow-loop: ## Run one complete slow-loop pass (no credentials)
	USE_FAKE_AGENTS=true $(UV) run firstdue slow-loop

# ------------------------------------------------------------- live mode ---
#
# `demo` and `slow-loop` above are fake mode: deterministic in-process adapters,
# no network, no cost. These two are the opposite -- real Gemini and Gemma on
# Vertex, real municipal and federal feeds, real Firestore. Both read `.env`
# for API keys and `.env.live` for everything else; `.env.live` is gitignored
# because it carries the callback secret.
#
# `FIRESTORE_NAMESPACE=local_` in that file keeps a local run out of the
# collections the deployed console reads. Clear it only deliberately.

.PHONY: live-loop
live-loop: ## One slow-loop pass with real models and real sources
	@[ -f .env.live ] || { echo "missing .env.live - see docs/setup.md"; exit 1; }
	@echo "live mode: Vertex AI, live municipal feeds, real Firestore"
	@set -a && . ./.env && . ./.env.live && set +a && \
	 $(UV) run firstdue slow-loop --district $(DISTRICT)

.PHONY: live-serve
live-serve: ## Start the API in live mode (no console; see the target's note)
	@[ -f .env.live ] || { echo "missing .env.live - see docs/setup.md"; exit 1; }
	@echo "live mode API on http://localhost:$(API_PORT)"
	@echo "the console is not started: in live mode the backend verifies a"
	@echo "Google OIDC token, and a laptop has no metadata server to mint one."
	@echo "use \`make demo\` for the UI, or the deployed console."
	@set -a && . ./.env && . ./.env.live && set +a && \
	 PORT=$(API_PORT) $(UV) run firstdue serve

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
test: ## Backend tests (contract tests skip without GCP_TEST_PROJECT_ID)
	$(UV) run pytest

.PHONY: test-cloud
test-cloud: ## Contract tests against a real Firestore and Pub/Sub. Needs GCP_TEST_PROJECT_ID.
	@if [ -z "$(GCP_TEST_PROJECT_ID)" ]; then \
	  echo "GCP_TEST_PROJECT_ID is not set."; \
	  echo "usage: make test-cloud GCP_TEST_PROJECT_ID=your-test-project"; \
	  echo "auth:  gcloud auth application-default login"; \
	  exit 1; \
	fi
	FIRESTORE_TEST_PROJECT=$(GCP_TEST_PROJECT_ID) \
	PUBSUB_TEST_PROJECT=$(GCP_TEST_PROJECT_ID) \
	FIRESTORE_TEST_DATABASE=$(GCP_TEST_FIRESTORE_DATABASE) \
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
	@# The image runs fake mode by default, so serving /healthz proves nothing
	@# about live mode -- and live mode is the only mode Cloud Run uses. The
	@# first real deployment exited(1) on `No module named google` in all eleven
	@# services with this target passing. Import what live mode needs.
	docker exec firstdue-smoke python -c "import google.cloud.firestore, google.cloud.pubsub, google.genai, langgraph; print('live-mode imports ok')"
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
