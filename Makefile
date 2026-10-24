.PHONY: install doctor lint run test chat

install:
	uv venv --allow-existing && uv pip install -e ".[dev]"

doctor:
	.venv/bin/ompwire doctor

lint:
	.venv/bin/ruff check src tests

run:
	.venv/bin/ompwire gateway run telegram

test:
	.venv/bin/python -m pytest tests/ -q

chat:
	.venv/bin/ompwire chat
