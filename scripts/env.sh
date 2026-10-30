# Preparación del entorno de Python, compartida por run.sh y por el lanzador
# de escritorio (./hangar-app). Se carga con «source»; no se ejecuta sola.
#
#   prepare_env requirements.txt [requirements-desktop.txt …]
#
# Crea .venv la primera vez, lo repara si quedó a medias y reinstala las
# dependencias cuando cambia alguno de los ficheros (p. ej. tras un git pull).

fail() { echo -e "\n\e[31mError:\e[0m $*\n" >&2; exit 1; }

prepare_env() {
    # --- Requisitos del sistema -----------------------------------------------
    command -v python3 >/dev/null || fail "falta Python 3: sudo apt install python3"
    python3 -c "import venv, ensurepip" 2>/dev/null ||
        fail "falta el módulo venv de Python: sudo apt install python3-venv"
    # libvirt-python se usa desde el paquete del sistema (el venv lo ve gracias
    # a --system-site-packages); compilarlo con pip necesitaría libvirt-dev y gcc
    python3 -c "import libvirt" 2>/dev/null ||
        fail "falta la librería de libvirt para Python: sudo apt install python3-libvirt"

    # --- Entorno virtual --------------------------------------------------------
    # Si un intento anterior falló a medias, .venv existe pero está incompleto
    if [[ -d .venv && ! -x .venv/bin/pip ]]; then
        echo "El entorno virtual (.venv) está incompleto: se vuelve a crear."
        rm -rf .venv
    fi
    if [[ ! -d .venv ]]; then
        echo "Creando el entorno virtual (.venv)…"
        python3 -m venv --system-site-packages .venv
    fi

    # Un sello por conjunto de ficheros: se reinstala si cambian o falta uvicorn
    local stamp wanted
    stamp=".venv/.requirements-$(echo "$*" | tr ' /' '__').sha256"
    wanted=$(cat "$@" | sha256sum | cut -d' ' -f1)
    if [[ ! -x .venv/bin/uvicorn || "$(cat "$stamp" 2>/dev/null)" != "$wanted" ]]; then
        echo "Instalando dependencias ($*)…"
        rm -f "$stamp"
        local args=()
        for f in "$@"; do args+=(-r "$f"); done
        .venv/bin/pip install -q --disable-pip-version-check "${args[@]}" ||
            fail "no se pudieron instalar las dependencias (¿hay conexión a Internet?).
       Vuelve a intentarlo cuando lo arregles; retomará la instalación."
        echo "$wanted" > "$stamp"
    fi
}
