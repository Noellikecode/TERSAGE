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
GCP_PROJECT ?= firstdue-dev
API_PORT   ?= 8000
FRONT_PORT ?= 3000

# The browser's Maps key, for Google's Photorealistic 3D Tiles.
#
# It is a *separate variable* from the backend's `GOOGLE_MAPS_API_KEY` on
# purpose. Every other credential here stays server-side; this one is inlined
# into the client bundle, because the tile renderer streams straight from
# `tile.googleapis.com` as the camera moves and proxying that would put the
# console in the path of every tile. So the key is public by construction, and
# it MUST be HTTP-referrer restricted in the Cloud console -- an unrestricted
# browser key is billable by whoever finds it.
#
# Falls back to the backend key from `.env` so the demo runs out of the box.
# That fallback is a convenience for a local demo, not a deployment pattern.
MAPS_BROWSER_KEY ?= $(shell grep -h '^GOOGLE_MAPS_API_KEY=' .env 2>/dev/null | tail -1 | cut -d= -f2-)

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
	@# Refuse a port already in use rather than losing the bind quietly. Uvicorn
	@# prints "address already in use" and exits while the *old* process keeps
	@# serving -- so a config change appears to have no effect, and the thing on
	@# screen is a process started with different settings. Cost us three
	@# debugging rounds; the check is one line.
	@lsof -ti:$(API_PORT) >/dev/null 2>&1 && { echo "port $(API_PORT) is in use; stop it first"; exit 1; } || true
	@trap 'kill 0' EXIT INT TERM; \
	 USE_FAKE_AGENTS=true DEMO_PRIME_SLOW_LOOP=true PORT=$(API_PORT) $(UV) run firstdue serve & \
	 cd $(FRONT) && \
	   FIRSTDUE_CONSOLE_TOKEN=$$($(UV) run --directory .. firstdue status | awk '/^  chief/{print $$2}') \
	   NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=$(MAPS_BROWSER_KEY) \
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

.PHONY: live-demo
live-demo: ## Live mode WITH the console: real models, real sources, real Firestore
	@[ -f .env.live ] || { echo "missing .env.live - see docs/setup.md"; exit 1; }
	@echo "==> minting a console token"
	@# Live mode verifies a Google OIDC token against the incident service's
	@# custom audience, and a bare user credential cannot choose an audience --
	@# only a service account can. So the console's credential is minted by
	@# impersonating `fd-ci-smoke`, the same identity the staging smoke suite
	@# uses, and handed to the console as FIRSTDUE_CONSOLE_TOKEN. On Cloud Run
	@# this comes from the metadata server; a laptop has none, which is the only
	@# reason this target exists.
	@#
	@# The token lasts an hour. Re-run the target to refresh it.
	@#
	@# The console waits for the API rather than starting beside it. Live mode
	@# checks every seeded profile against Firestore before it accepts traffic,
	@# which is the better part of a minute -- and a console started in parallel
	@# spends that minute rendering 503s that look like a broken backend rather
	@# than a slow one.
	@#
	@# ---- everything that has a real counterpart, actually used ----
	@#
	@# These override `.env.live` for this target only, so the demo runs on the
	@# real services rather than on the cheaper local stand-ins:
	@#
	@#   PUBSUB_TOPIC_PREFIX=
	@#     Empty, because that is what Terraform actually created. The module
	@#     names topics `replace(each.value, ".", "-")` with no prefix, while the
	@#     setting defaults to `firstdue` -- so the app addressed
	@#     `firstdue-fact-written` and the project holds `fact-written`. Every
	@#     publish went nowhere, silently, because the publish future was never
	@#     resolved. Both halves are fixed; this sets the name to match.
	@#
	@#   EVENT_BACKEND=pubsub + PUBSUB_PULL_BRIDGE=true
	@#     Real Pub/Sub. Production delivery is push to an authenticated Cloud
	@#     Run endpoint and Google cannot push to localhost, so the bridge pulls
	@#     from its own `local-demo-*` subscriptions and delivers through the
	@#     same handler the push endpoint uses. It never touches the deployed
	@#     push subscriptions. See `firstdue.adapters.pubsub.pull`.
	@#
	@#   CENTRAL_DATABASE_ENABLED=false
	@#     Permits, the assessor's roll, inspections and violations come from
	@#     DataSF over HTTP instead of the generated corpus in Firestore. Real
	@#     rows, and thinner ones: the corpus exists because the narrative half
	@#     of a municipal record is not published. Set it back to `true` for a
	@#     demo that needs the inspector prose.
	@#
	@#   GROUNDING_SEARCH_ENABLED is deliberately NOT set.
	@#     Measured against this project on 2026-08-27: every grounded call
	@#     through the adapter fails. Given a 45-second deadline -- seven times
	@#     the 6s the callers actually allow -- four calls took 33-45s and all
	@#     four declined, and a slow-loop pass filled the log with
	@#     `grounding_call_failed` and ran for a full minute doing nothing.
	@#
	@#     It is not a deadline problem. The same search, called directly over
	@#     REST with `googleSearch` on the v1 endpoint, answers in 3.3s. The
	@#     adapter goes through the Gen AI SDK against v1beta1 with automatic
	@#     function calling enabled, and that is where it dies -- worth fixing,
	@#     and not worth blocking a demo on.
	@#
	@#     Off, the grounding service returns its documented *unavailable*
	@#     state: it declines every reference with a reason and returns no
	@#     reports, which is the truthful answer to "what did the web say" when
	@#     nobody successfully asked the web.
	@#
	@#   DEMO_SYNTHETIC_SWEEP=true
	@#     Lets `sensor-fusion` fly the generated drone sweep against the real
	@#     vision model. Without it that sweep is refused -- the frames are
	@#     generated, and an unlabelled real reading of a generated building is
	@#     indistinguishable from a real reading of a real one -- so the agent
	@#     that reads walls does nothing for the whole demo and reads as idle.
	@#
	@#     The refusal is about *disclosure*, not about the frame being
	@#     generated, so this is the same bargain the simulated 911 call makes:
	@#     opt-in at launch, never inferred from the mode, and every frame, log
	@#     entry and audit record marked `synthetic-drone` with SIMULATED in the
	@#     headline an officer actually reads. Do not set it on a real
	@#     deployment: it would put simulated wall readings in a real fire's
	@#     permanent record.
	@#
	@#   FIRESTORE_NAMESPACE=demo_<epoch>_ + DEMO_REBUILD_FINDINGS=true
	@#     A blank slate for every launch, which is the only way the slow loop has
	@#     anything to show. `.env.live` pins `local_`, and that one namespace
	@#     accumulated across every run this project has ever done -- 134 profiles,
	@#     10,674 facts and 32 conflicts, all present before the console finished
	@#     loading. A pass over a finished district is a no-op by construction: the
	@#     facts are already appended and the conflicts already recorded, so the
	@#     counters cannot move and the screen shows five idle agents standing over
	@#     work somebody else did. Measured before this line existed: 121 s of a
	@#     live pass, 56 materialisations, every district counter flat.
	@#
	@#     The stamped namespace gives each launch its own collections, so the seed
	@#     lands fresh. `DEMO_REBUILD_FINDINGS` then withholds the seeded conflicts
	@#     from that load, leaving the *records* -- the months of reading the fleet
	@#     is meant to have already done -- and none of the disagreements. The
	@#     "records disagree" panel opens empty and `structure-watch` fills it in on
	@#     its first pass, from deterministic rules over facts already in the store,
	@#     with no model call and no wait.
	@#
	@#     It leaves a namespace per run in Firestore. That is a dev project and
	@#     they are small; `make demo` in fake mode leaves nothing at all.
	@#
#   NEXT_PUBLIC_DEMO_DISPATCH=true (console)
	@#     Runs the demo choreography against the live backend: slow-loop
	@#     passes on an interval, then a simulated 911 call, then the drone
	@#     sweep and the agency notifications. The console refuses to place a
	@#     call on a live backend unless this says so -- software that invented
	@#     a 911 call on a real deployment would be indefensible, so it is
	@#     opt-in at launch rather than inferred from the mode. Every banner it
	@#     produces still says the call is synthetic.
	@#
	@#   INTERNAL_PUSH_AUDIENCE / INTERNAL_PUSH_SERVICE_ACCOUNT
	@#     The same identities the deployed incident service verifies. Pub/Sub
	@#     mode refuses to start without them, and rightly: the push endpoint is
	@#     mounted either way, and one that cannot check who called it is an open
	@#     door into the fleet's event stream. Nothing pushes to a laptop -- the
	@#     bridge pulls -- but the door is still shut the same way.
	@#
	@# Still not real, and not fixable from here: Workspace Calendar and Gmail
	@# need domain-wide delegation a personal account cannot grant; Vertex
	@# Vector Search needs a deployed index endpoint that bills monthly; and the
	@# four inter-agency write targets have no real counterpart to write to,
	@# which the console's own disclosure says.
	@trap 'kill 0' EXIT INT TERM; \
	 TOKEN=$$(gcloud auth print-identity-token \
	   --impersonate-service-account=fd-ci-smoke@$(GCP_PROJECT).iam.gserviceaccount.com \
	   --audiences=https://firstdue-incident --include-email 2>/dev/null); \
	 [ -n "$$TOKEN" ] || { echo "could not mint a token; see infra/smoke-staging.sh for the grant it needs"; exit 1; }; \
	 lsof -ti:$(API_PORT) >/dev/null 2>&1 && { echo "port $(API_PORT) is already in use; stop it first"; exit 1; } || true; \
	 ( set -a && . ./.env && . ./.env.live && set +a && \
	   FIRESTORE_NAMESPACE=demo_$$(date +%s)_ DEMO_REBUILD_FINDINGS=true \
	   EVENT_BACKEND=pubsub PUBSUB_PULL_BRIDGE=true PUBSUB_TOPIC_PREFIX= \
	   INTERNAL_PUSH_AUDIENCE=https://firstdue-incident \
	   INTERNAL_PUSH_SERVICE_ACCOUNT=fd-pubsub-push@$(GCP_PROJECT).iam.gserviceaccount.com,fd-scheduler@$(GCP_PROJECT).iam.gserviceaccount.com \
	   CENTRAL_DATABASE_ENABLED=false \
	   DEMO_SYNTHETIC_SWEEP=true \
	   PORT=$(API_PORT) $(UV) run firstdue serve ) & \
	 echo "==> waiting for the API (seeding a fresh district into Firestore, ~2 min)"; \
	 for i in $$(seq 1 180); do \
	   curl -sf http://localhost:$(API_PORT)/readyz >/dev/null 2>&1 && break; \
	   sleep 2; \
	 done; \
	 curl -sf http://localhost:$(API_PORT)/readyz >/dev/null 2>&1 || { echo "the API did not come up; see the log above"; exit 1; }; \
	 echo "  api     http://localhost:$(API_PORT)"; \
	 echo "  console http://localhost:$(FRONT_PORT)"; \
	 ( cd $(FRONT) && FIRSTDUE_CONSOLE_TOKEN=$$TOKEN \
	   NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=$(MAPS_BROWSER_KEY) \
	   NEXT_PUBLIC_DEMO_DISPATCH=true \
	   NEXT_PUBLIC_API_BASE_URL=http://localhost:$(API_PORT) $(NPM) run dev -- -p $(FRONT_PORT) ) & \
	 wait

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
