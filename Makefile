.PHONY: check test-fast
check:
	uv run --locked ruff format --check .
	uv run --locked ruff check .
	uv run --locked ty check
	uv run --locked pytest -n auto

test-fast:
	uv run --locked pytest -n auto -m "not slow"
