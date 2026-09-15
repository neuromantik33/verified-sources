.PHONY: install-poetry has-poetry dev lint test pg-up pg-down test-pg-replication
.SILENT:has-poetry

# Postgres major version the replication tests run against. Any debezium/postgres
# tag works, 9.6 through 17. 9.6 is the default because it hits the pre-10 paths.
PG_VERSION ?= 9.6
export PG_VERSION

PG_COMPOSE = docker compose -f tests/postgres/docker-compose.yml
PG_TEST_ENV = ALL_DESTINATIONS='["duckdb", "postgres"]' \
	DESTINATION__POSTGRES__CREDENTIALS=postgresql://loader:loader@localhost:5432/dlt_data

help:
	@echo "make"
	@echo "		install-poetry"
	@echo "			installs newest poetry version"
	@echo "		dev"
	@echo "			prepares development env"
	@echo "		lint"
	@echo "			runs flake and mypy on all sources"
	@echo "		test"
	@echo "			tests all the components including destinations"

install-poetry:
ifneq ($(VIRTUAL_ENV),)
	$(error you cannot be under virtual environment $(VIRTUAL_ENV))
endif
	curl -sSL https://install.python-poetry.org | python3 - --version 1.8.5

has-poetry:
	poetry --version

dev: has-poetry
	poetry install --without unstructured_data

lint-dlt-init:
	poetry run ./check-requirements.py
	poetry run pytest tests/test_dlt_init.py --no-header

lint-code:
	./check-package.sh
	poetry run mypy --config-file mypy.ini ./sources
	# poetry run mypy --config-file mypy.ini ./tests
	poetry run mypy --config-file mypy.ini ./tools
	poetry run flake8 --max-line-length=200 --extend-ignore=W503 sources init --show-source
	poetry run flake8 --max-line-length=200 --extend-ignore=W503 tests --show-source
	poetry run black ./ --diff

lint: lint-code lint-dlt-init

format:
	poetry run black ./

format-lint: format lint

test:
	poetry run pytest tests

test-local:
	$(PG_TEST_ENV) poetry run pytest tests

pg-up:
	$(PG_COMPOSE) up -d --wait

pg-down:
	$(PG_COMPOSE) down -v

# Runs the replication tests against PG_VERSION, e.g.
#   make test-pg-replication                  # 9.6
#   make test-pg-replication PG_VERSION=14
test-pg-replication: pg-up
	$(PG_TEST_ENV) poetry run pytest tests/pg_legacy_replication
