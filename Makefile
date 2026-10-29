.PHONY: check test-fast
check:
	uv run --locked --extra extraction ruff format --check .
	uv run --locked --extra extraction ruff check .
	uv run --locked --extra extraction ty check
	uv run --locked --extra extraction pytest -n auto

test-fast:
	uv run --locked --extra extraction pytest -n auto -m "not slow"
