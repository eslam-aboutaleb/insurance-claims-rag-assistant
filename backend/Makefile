# Targets consumed by the central ci-workflows integration contract.
# PostgreSQL and Redis are disposable services provided by that workflow.

CI_TEST_DATABASE_URL = $(patsubst postgresql://%,postgresql+asyncpg://%,$(CI_POSTGRES_URL))

.PHONY: ci-install ci-migrate ci-integration

ci-install:
	python -m pip install -e .

ci-migrate:
	test -n "$(CI_POSTGRES_URL)"
	DATABASE_URL="$(CI_TEST_DATABASE_URL)" python -m alembic upgrade head

ci-integration:
	test -n "$(CI_POSTGRES_URL)"
	OMNICARE_TEST_DATABASE_URL="$(CI_TEST_DATABASE_URL)" DATABASE_URL="$(CI_TEST_DATABASE_URL)" REDIS_URL="$(CI_REDIS_URL)" ENVIRONMENT=test python -m pytest tests -q
