# OmniCare Financial — Phase 0 baseline targets.
#
# Each target runs one black-box tier of the baseline harness
# and writes its report under .kilo/baseline/ (Tier A/B) or
# frontend/e2e/journeys/artifacts/ (Tier C).
#
# Tier B and Tier C need a live stack:
#   docker compose up --build
#
# Usage:
#   make baseline:tier-a    # Tier A: frozen HTTP contract suite
#   make baseline:tier-b    # Tier B: DB-backed integration suite
#   make baseline:tier-c    # Tier C: Playwright user journeys (real LLM)
#   make baseline:all       # all three, in order
#
# Override the Tier B database with:
#   make baseline:tier-b TEST_DB_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/omnicare_test

BACKEND_DIR := backend
FRONTEND_DIR := frontend

# The compose stack publishes PostgreSQL on 15432 by default.
TEST_DB_URL ?= postgresql+asyncpg://omnicare:omnicare_password@127.0.0.1:15432/omnicare_test

# Prefer the backend virtualenv when it exists.
PYTHON := $(shell if [ -x "$(BACKEND_DIR)/.venv/bin/python" ]; then echo "$(BACKEND_DIR)/.venv/bin/python"; else echo python3; fi)

.PHONY: baseline:tier-a baseline:tier-b baseline:tier-c baseline:all

baseline:tier-a:
	cd $(BACKEND_DIR) && ./scripts/baseline.sh --contract

baseline:tier-b:
	cd $(BACKEND_DIR) && OMNICARE_TEST_DATABASE_URL="$(TEST_DB_URL)" $(PYTHON) -m pytest tests/integration -v

baseline:tier-c:
	cd $(FRONTEND_DIR) && ./scripts/journeys.sh

baseline:all: baseline:tier-a baseline:tier-b baseline:tier-c
