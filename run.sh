#!/bin/bash
# Arranca Hangar en el navegador: http://127.0.0.1:8090 (solo este equipo).
# Para usarlo como aplicación con su propia ventana, usa ./hangar-app.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
source scripts/env.sh

prepare_env requirements.txt

exec .venv/bin/uvicorn hangar.main:app --host 127.0.0.1 --port "${PORT:-8090}" "$@"
