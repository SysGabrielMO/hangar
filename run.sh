#!/bin/bash
# Arranca Hangar en http://127.0.0.1:8090 (solo accesible desde este equipo).
# La primera vez crea el entorno virtual e instala las dependencias.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

if [[ ! -d .venv ]]; then
    # --system-site-packages para usar python3-libvirt del sistema
    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/uvicorn hangar.main:app --host 127.0.0.1 --port "${PORT:-8090}" "$@"
