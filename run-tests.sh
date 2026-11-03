#!/bin/bash
# Ejecuta la suite de tests (crea el entorno si hace falta).
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
source scripts/env.sh

prepare_env requirements.txt requirements-dev.txt
exec .venv/bin/pytest "$@"
