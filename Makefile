.DEFAULT_GOAL := help

.PHONY: help lint format unit pre-commit clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# Repo-wide, not just charmtally/: .github/ carries Python too, and CI now
# runs this exact target, so anything scoped narrower would go unchecked.
lint:  ## Lint with ruff, ty and codespell
	uv run --group dev ruff check
	uv run --group dev ruff format --check
	uv run --group dev ty check
	uv run --group dev codespell .

format:  ## Format and auto-fix with ruff
	uv run --group dev ruff format
	uv run --group dev ruff check --fix

unit:  ## Run the unit tests
	uv run --group dev pytest

pre-commit:  ## Run all pre-commit hooks against every file
	uv run --group dev pre-commit run --all-files

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
