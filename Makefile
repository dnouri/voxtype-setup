.DEFAULT_GOAL := help
PYTHON ?= python3
INSTALL_FLAGS ?=
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: help plan install doctor test check
help:
	@printf '%s\n' \
	  'make plan     Read-only installation plan (the default installer behaviour)' \
	  'make install  Explicitly apply; never replaces differing files implicitly' \
	  '              Optional: INSTALL_FLAGS=--refresh-desktop (runs kbuildsycoca6)' \
	  '              Explicit replacements: INSTALL_FLAGS=--replace (private backups)' \
	  'make doctor   Read-only configuration and prerequisite checks' \
	  'make test     Offline tests; no real desktop, services, clipboard, or microphone' \
	  'make check    Tests plus syntax checks (Python 3.11+ and Bash required)'

plan:
	$(PYTHON) scripts/install.py

install:
	$(PYTHON) scripts/install.py --apply $(INSTALL_FLAGS)

doctor:
	$(PYTHON) scripts/doctor.py

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test
	bash -n scripts/record-sample.sh
	$(PYTHON) -c 'import ast, pathlib; [ast.parse(p.read_text(), filename=str(p)) for d in ("scripts", "tests") for p in pathlib.Path(d).glob("*.py")]; print("Python syntax: OK")'
