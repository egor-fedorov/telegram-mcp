.PHONY: check
check:
	uv run --locked ruff format --check .
	uv run --locked ruff check .
	uv run --locked ty check
	uv run --locked actionlint
	uv run --locked zizmor --offline .github
	uv run --locked pytest
