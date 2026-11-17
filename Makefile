# schem2spice: install, test and run.
#   make install                 everything: ngspice, Python env, OCR models, checks
#   make test                    engine tests (no manual needed)
#   make test-all                also the tests that read the reference project's scan
#   make ingest PROJECT=...      PDF -> page images for a project
#   make chain  PROJECT=...      build and simulate a project's board chain, draw its charts
# PROJECT defaults to the reference project, projects/marantz-pm80mk2.

PYTHON  ?= python3
VENV    ?= .venv
PROJECT ?= projects/marantz-pm80mk2
CHAIN   ?= systems/chain.yaml

BIN := $(VENV)/bin
S2S := $(BIN)/schem2spice --project $(PROJECT)
OS  := $(shell uname -s)

.DEFAULT_GOAL := install
.PHONY: install tools python venv ocr-models doctor test test-all ingest chain plots clean clean-venv help

help:
	@sed -n '2,7p' Makefile | sed 's/^# //'

install: tools venv ocr-models doctor

## --- external tools -----------------------------------------------------------

tools: python
	@if command -v ngspice >/dev/null 2>&1; then \
	  echo "ngspice: $$(command -v ngspice)"; \
	elif [ "$(OS)" = "Darwin" ]; then \
	  command -v brew >/dev/null 2>&1 || { echo "Homebrew is needed: https://brew.sh"; exit 1; }; \
	  brew install ngspice; \
	elif command -v apt-get >/dev/null 2>&1; then \
	  sudo apt-get update && sudo apt-get install -y ngspice; \
	elif command -v dnf >/dev/null 2>&1; then \
	  sudo dnf install -y ngspice; \
	else \
	  echo "Install ngspice (>= 39) with your package manager: https://ngspice.sourceforge.io"; exit 1; \
	fi

python:
	@$(PYTHON) -c 'import sys; v = sys.version_info; sys.exit(0 if v >= (3, 11) else 1)' || { \
	  echo "Python >= 3.11 is needed (found: $$($(PYTHON) --version 2>&1)). Set PYTHON=python3.12 or install one:"; \
	  echo "  macOS: brew install python@3.12    Debian/Ubuntu: sudo apt-get install python3.12-venv"; exit 1; }

## --- Python environment -----------------------------------------------------

venv: $(BIN)/schem2spice

$(BIN)/schem2spice: pyproject.toml | python
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e '.[dev]'
	@touch $@

# RapidOCR fetches its ONNX models on first use: do it now, while online.
ocr-models: venv
	$(BIN)/python -c 'import numpy as np; from rapidocr import RapidOCR; RapidOCR()(np.full((64, 256, 3), 255, np.uint8)); print("RapidOCR models ready")'

doctor: venv
	-@$(S2S) doctor

## --- tests ------------------------------------------------------------------

# Without the manual, tests that read the scan skip themselves.
test: venv
	$(BIN)/pytest -q

test-all: venv
	@$(S2S) doctor >/dev/null || { echo "The reference project needs its PDF and pages: see $(PROJECT)/README.md"; exit 1; }
	$(BIN)/pytest -q -rs

## --- a project ----------------------------------------------------------------

ingest: venv
	$(S2S) ingest

chain: venv
	$(S2S) system --file $(CHAIN) --build

# Redraw the chain's charts from its last run (no simulation).
plots: venv
	$(S2S) plot --file $(CHAIN)

## --- cleanup ----------------------------------------------------------------

# Generated output of PROJECT (keeps its PDF, page images and OCR cache).
clean:
	find $(PROJECT)/out -mindepth 1 -maxdepth 1 ! -name cache -exec rm -rf {} +
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache

clean-venv:
	rm -rf $(VENV) *.egg-info src/*.egg-info
