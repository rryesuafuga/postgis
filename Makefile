SHELL := /bin/bash
PY ?= python3

# .env is read by scripts/lib.sh and ews/config.py, and never overrides variables already
# exported in the shell (e.g. `PGPORT=5433 make demo`).

# The offline demo uses its own data directory AND its own database, so synthetic test data
# can never mix with real downloads or real results.
DEMO := EWS_DATA_DIR=data/synthetic PGDATABASE=ews_demo
IMERG_LIKE := data/synthetic/raw/synthetic_imerg_like_daily.nc
CAP_XSD_URL := https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.xsd

.PHONY: help up down init fetch load compute alert all app status integrity backup reingest site \
        report test cap-xsd demo demo-data demo-app

help:
	@echo "Real data (needs internet):  make up init fetch load compute alert integrity app"
	@echo "                             or: make up all app"
	@echo "Offline synthetic demo:      make up demo demo-app"
	@echo "Operations:                  make status | integrity | backup | report | reingest DATE=YYYY-MM-DD"
	@echo "Tests:                       make test"

up:
	docker compose up -d db

down:
	docker compose down

init:
	bash scripts/00_init_db.sh

# --- real data ------------------------------------------------------------------------------
fetch:
	$(PY) -m ews fetch-districts
	$(PY) -m ews fetch-chirps
	$(PY) -m ews fetch-climatology
	$(PY) -m ews climatology

load:
	bash scripts/10_load_districts.sh
	bash scripts/20_load_rasters.sh

compute:
	$(PY) -m ews compute

alert:
	$(PY) -m ews alert

all: init fetch load compute alert integrity

app:
	$(PY) -m streamlit run app/streamlit_app.py

# --- operations -----------------------------------------------------------------------------
status:
	$(PY) -m ews status

integrity:
	$(PY) -m ews integrity

backup:
	bash scripts/backup.sh

reingest:
	@test -n "$(DATE)" || (echo "usage: make reingest DATE=YYYY-MM-DD [ARGS=--local]"; exit 2)
	bash scripts/reingest_date.sh $(DATE) $(ARGS)

report:
	$(PY) -m ews report $(if $(MONTH),--month $(MONTH),)

cap-xsd:
	mkdir -p schemas && curl -fsSL -o schemas/CAP-v1.2.xsd $(CAP_XSD_URL)

test:
	$(PY) -m pytest -q tests

# --- offline synthetic demo -----------------------------------------------------------------
demo-data:
	$(DEMO) $(PY) -m ews synthetic
	$(DEMO) $(PY) -m ews climatology
	$(DEMO) $(PY) -m ews prep-aux --input $(IMERG_LIKE) --name imerg_like --units mm/day

demo: demo-data
	$(DEMO) bash scripts/00_init_db.sh
	$(DEMO) bash scripts/10_load_districts.sh
	$(DEMO) bash scripts/20_load_rasters.sh
	$(DEMO) $(PY) -m ews compute
	$(DEMO) bash scripts/21_load_aux.sh imerg_like
	$(DEMO) $(PY) -m ews alert --update-if-active
	$(DEMO) bash scripts/backup.sh
	$(DEMO) $(PY) -m ews integrity

demo-app:
	$(DEMO) $(PY) -m streamlit run app/streamlit_app.py

# Static page for GitHub Pages (Settings > Pages > branch main, folder /docs), from the demo database.
site:
	$(DEMO) $(PY) -m ews site
