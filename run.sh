#!/bin/bash
# Arranca Hangar en http://127.0.0.1:8090 (solo accesible desde este equipo).
# Prepara el entorno virtual la primera vez, lo repara si quedó a medias y
# reinstala las dependencias cuando cambia requirements.txt (tras un git pull).
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

fail() { echo -e "\n\e[31mError:\e[0m $*\n" >&2; exit 1; }

# --- Requisitos del sistema ---------------------------------------------------
command -v python3 >/dev/null || fail "falta Python 3: sudo apt install python3"
python3 -c "import venv, ensurepip" 2>/dev/null ||
    fail "falta el módulo venv de Python: sudo apt install python3-venv"
# libvirt-python se usa desde el paquete del sistema (el venv lo ve gracias a
# --system-site-packages); compilarlo con pip necesitaría libvirt-dev y gcc
python3 -c "import libvirt" 2>/dev/null ||
    fail "falta la librería de libvirt para Python: sudo apt install python3-libvirt"

# --- Entorno virtual ----------------------------------------------------------
# Si un intento anterior falló a medias, .venv existe pero está incompleto
if [[ -d .venv && ! -x .venv/bin/pip ]]; then
    echo "El entorno virtual (.venv) está incompleto: se vuelve a crear."
    rm -rf .venv
fi
if [[ ! -d .venv ]]; then
    echo "Creando el entorno virtual (.venv)…"
    python3 -m venv --system-site-packages .venv
fi

# Se reinstala si cambia requirements.txt o si falta uvicorn
stamp=.venv/.requirements.sha256
wanted=$(sha256sum requirements.txt | cut -d' ' -f1)
if [[ ! -x .venv/bin/uvicorn || "$(cat "$stamp" 2>/dev/null)" != "$wanted" ]]; then
    echo "Instalando dependencias…"
    rm -f "$stamp"
    .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt ||
        fail "no se pudieron instalar las dependencias (¿hay conexión a Internet?).
       Vuelve a ejecutar ./run.sh cuando lo arregles; retomará la instalación."
    echo "$wanted" > "$stamp"
fi

exec .venv/bin/uvicorn hangar.main:app --host 127.0.0.1 --port "${PORT:-8090}" "$@"
