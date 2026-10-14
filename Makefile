.PHONY: all install nopyc clean test check schema local validate-release upload mcpb

SHELL := /usr/bin/env bash
PYTHON_BIN ?= python
PROJECT_VENV ?= venv
MCPB_VERSION ?= 2.1.2

all: local check test

venv:
	$(PYTHON_BIN) -m pip install virtualenv --user
	$(PYTHON_BIN) -m virtualenv $(PROJECT_VENV)

install: venv
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		python -m pip install .; \
	)

nopyc:
	find . -name '*.pyc' | xargs rm -f || true
	find . -name __pycache__ | xargs rm -rf || true

clean: nopyc
	rm -rf build dist *.egg-info $(PROJECT_VENV)

test: install
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		python -m pip install ".[dev]"; \
		coverage run -m pytest -v && coverage report && coverage xml && coverage html; \
	)

check: install
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		python -m pip install ".[dev]"; \
		mypy amazonorders_mcp && flake8; \
	)

schema: install
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		python -m pip install ".[schema]"; \
		python scripts/generate-schema.py; \
	)

local:
	@rm -rf *.egg-info dist
	@( \
		$(PYTHON_BIN) -m pip install --upgrade pip && \
		$(PYTHON_BIN) -m pip install --upgrade build && \
		$(PYTHON_BIN) -m build && \
		$(PYTHON_BIN) -m pip install "$$(ls dist/*.tar.gz)"; \
	)

validate-release:
	@if [[ "${VERSION}" == "" ]]; then echo "VERSION is not set" & exit 1 ; fi

	@if [[ $$(grep "__version__ = \"${VERSION}\"" amazonorders_mcp/__init__.py) == "" ]] ; then echo "Version not bumped in amazonorders_mcp/__init__.py" & exit 1 ; fi

upload: local
	@( \
		$(PYTHON_BIN) -m pip install --upgrade twine && \
		$(PYTHON_BIN) -m build && \
		$(PYTHON_BIN) -m twine upload dist/*; \
	)

mcpb:
	@( \
		$(PYTHON_BIN) scripts/build-mcpb.py && \
		npx -y @anthropic-ai/mcpb@$(MCPB_VERSION) validate build/mcpb/manifest.json && \
		npx -y @anthropic-ai/mcpb@$(MCPB_VERSION) pack build/mcpb "build/amazon-orders-mcp-$$($(PYTHON_BIN) -c 'import amazonorders_mcp; print(amazonorders_mcp.__version__)').mcpb"; \
	)
