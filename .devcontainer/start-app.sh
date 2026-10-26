#!/usr/bin/env bash
# Serve the Streamlit app on port 8501 inside the dev container / codespace.
#   bash .devcontainer/start-app.sh         synthetic demo (database ews_demo); run automatically
#                                            on attach, rebuilding the demo if the first setup failed
#   bash .devcontainer/start-app.sh real    real data (database ews), after `make init fetch load compute`
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-demo}"

for _ in $(seq 1 30); do
    pg_isready -q && break
    echo "waiting for the database..."; sleep 2
done

if [[ "$MODE" == real ]]; then
    export PGDATABASE=ews EWS_DATA_DIR=data
    if ! psql -X -At -c "SELECT 1 FROM ews.district_exceedance LIMIT 1" 2>/dev/null | grep -q 1; then
        echo "No real-data results yet. Run: make init fetch load compute alert" >&2
        exit 1
    fi
else
    export PGDATABASE=ews_demo EWS_DATA_DIR=data/synthetic
    if ! psql -X -At -c "SELECT 1 FROM ews.district_exceedance LIMIT 1" 2>/dev/null | grep -q 1; then
        echo "Demo database not found: building it now (about a minute)..."
        make demo
    fi
fi

echo "Starting the app ($PGDATABASE). It opens in a preview tab; for a full browser tab use the Ports panel (port 8501)."
# CORS/XSRF checks off: Codespaces serves the app through its own forwarding domain.
exec python -m streamlit run app/streamlit_app.py \
    --server.port 8501 --server.enableCORS false --server.enableXsrfProtection false
