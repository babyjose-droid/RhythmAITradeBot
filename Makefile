.PHONY: install lint typecheck contracts test test-unit check up down migrate

PY ?= .venv/bin

install:
	python3 -m venv .venv
	$(PY)/pip install --upgrade pip
	$(PY)/pip install -e ".[dev]"

lint:
	$(PY)/ruff check src tests
	$(PY)/ruff format --check src tests

typecheck:
	$(PY)/mypy

contracts:
	$(PY)/lint-imports

test-unit:
	$(PY)/pytest tests/unit

# Requires TEST_DATABASE_URL, e.g. postgresql+psycopg://rhythm:pw@localhost:5432/rhythm_test
test:
	REQUIRE_INTEGRATION=1 $(PY)/pytest --cov --cov-report=term

check: lint typecheck contracts test

migrate:
	$(PY)/alembic upgrade head

up:
	docker compose up --build -d

down:
	docker compose down
