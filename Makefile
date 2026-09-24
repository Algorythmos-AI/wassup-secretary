.PHONY: bootstrap lint fmt typecheck test-fast test ci up down

bootstrap:
	uv sync --all-packages
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check . --fix

typecheck:
	uv run mypy libs/wassup_core/src services/*/src

# Unit tests only: no database needed.
test-fast:
	uv run pytest -m "not db"

# Everything, including tests that need a real Postgres (set DATABASE_URL).
test:
	uv run pytest

ci: lint typecheck test

up:
	docker compose up --build

down:
	docker compose down -v
