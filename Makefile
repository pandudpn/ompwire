.PHONY: install doctor test chat lint

install:
	uv venv --allow-existing && uv pip install -e ".[telegram]"

doctor:
	.venv/bin/ompwire doctor

test:
	.venv/bin/python -m pytest tests/ -q

chat:
	.venv/bin/ompwire chat
