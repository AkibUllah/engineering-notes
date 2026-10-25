PYTHON ?= python3
VENV ?= .venv
PORT ?= 8080

.PHONY: install build serve clean

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

build:
	$(VENV)/bin/python build.py

serve:
	$(VENV)/bin/python build.py --serve $(PORT)

clean:
	rm -rf _site
