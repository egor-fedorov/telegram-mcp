.PHONY: check
check:
	uv run --locked ruff format --check .
	uv run --locked ruff check .
	uv run --locked ty check
	uv run --locked pytest
