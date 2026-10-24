.PHONY: install doctor lint serve test chat

install:
	uv venv --allow-existing && uv pip install -e ".[dev]"

doctor:
	.venv/bin/ompwire doctor

lint:
	.venv/bin/ruff check src tests

serve:
	.venv/bin/ompwire serve

test:
	.venv/bin/python -m pytest tests/ -q

chat:
	.venv/bin/ompwire chat
