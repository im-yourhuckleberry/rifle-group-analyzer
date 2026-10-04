# Rifle Group Analyzer: development entry points. All Python runs through .venv.
# First run: make bootstrap

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

VENV_PY := .venv/bin/python
PYTEST := .venv/bin/pytest

.PHONY: help bootstrap lock test test-fast accuracy smoke e2e scan run lint

help:
	@echo "targets: bootstrap lock test test-fast accuracy smoke e2e scan run lint"

$(VENV_PY):
	@echo "missing .venv: run 'make bootstrap' first" >&2; exit 1

bootstrap:
	./scripts/bootstrap.sh

lock:
	./scripts/lock.sh

test: $(VENV_PY)
	$(PYTEST) tests/unit tests/integration -q

test-fast: $(VENV_PY)
	$(PYTEST) tests/unit tests/integration -q -m "not accuracy"

accuracy: $(VENV_PY)
	$(VENV_PY) tools/eval_detection.py --catalog tests/synth/catalog.json
	$(PYTEST) tests/integration/test_detection_accuracy.py -q

smoke:
	./scripts/smoke.sh

e2e: $(VENV_PY)
	$(PYTEST) tests/e2e -q

scan:
	./scripts/scan.sh

run:
	./scripts/run.sh

lint: $(VENV_PY)
	.venv/bin/ruff check .
	for f in scripts/*.sh; do bash -n "$$f"; done
	if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/*.sh; else echo "SHELLCHECK: NOT INSTALLED"; fi
