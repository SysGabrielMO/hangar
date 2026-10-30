# Hangar

Aplicación para gestionar desde un solo sitio las máquinas KVM, los
contenedores LXC (`lxc-*`) y los contenedores Docker de este equipo. Se usa
como **aplicación de escritorio** (ventana propia) o desde el **navegador**.

Se ejecuta siempre con tu usuario, **nunca con sudo** (solo se usa sudo para
instalar paquetes y, si usas LXC, el ayudante del paso 4).

## Instalar la aplicación de escritorio, paso a paso

Probado en Debian 13; en Ubuntu los paquetes se llaman igual.

**1. Instala los paquetes del sistema.** Los imprescindibles:

```bash
sudo apt install git python3-venv python3-libvirt libxcb-cursor0 libnotify-bin
```

Y los de lo que vayas a gestionar (si ya usas KVM, LXC o Docker, ya los tendrás):

```bash
sudo apt install libvirt-daemon-system qemu-system-x86 qemu-utils virtinst virt-viewer
```

```bash
sudo apt install lxc docker.io
```

`virt-viewer` es lo que abre la «Pantalla» de las VMs. Si instalaste Docker
desde la web oficial de Docker, no instales `docker.io`.

**2. Dale permisos a tu usuario** sobre libvirt y Docker:

```bash
sudo usermod -aG libvirt,docker $USER
```

(Si no usas Docker, deja solo `libvirt`: el grupo `docker` no existe sin él.)
**Cierra sesión y vuelve a entrar** (o reinicia) para que se apliquen. Puedes
comprobarlo con `groups`: deben salir `libvirt` y `docker`.

**3. Descarga Hangar:**

```bash
git clone https://github.com/SysGabrielMO/hangar.git ~/hangar && cd ~/hangar
```

**4. (Solo si usas LXC)** instala el ayudante que permite gestionar los
contenedores de `/var/lib/lxc` sin ejecutar Hangar como root:

```bash
sudo ./scripts/install-lxc-helper.sh
```

**5. Instala la aplicación** (con tu usuario, sin sudo):

```bash
./scripts/install-desktop.sh
```

Crea el entorno de Python, descarga Qt (PySide6, unos 700 MB, solo la primera
vez) y añade **Hangar** al menú de aplicaciones con su icono.

**6. Ábrela** desde el menú de aplicaciones, como cualquier programa. También
se puede abrir desde la terminal con `./hangar-app`.

### Actualizar

```bash
cd ~/hangar && git pull
```

Al abrirla, Hangar instala solo las dependencias nuevas. Si cambia el
ayudante de LXC, repite el paso 4. Si mueves la carpeta, repite el paso 5.

### Desinstalar

```bash
./scripts/install-desktop.sh --remove
```

```bash
sudo rm -f /usr/local/sbin/hangar-lxc-helper /etc/sudoers.d/hangar
```

Y borra la carpeta `~/hangar`.

### Cómo funciona la aplicación

- El servidor arranca solo, en un puerto libre, y se apaga al cerrar la ventana.
- Solo esa ventana puede usarlo: entra con una clave aleatoria, así que otro
  usuario del mismo equipo no puede abrirlo aunque llegue a `127.0.0.1`.
- La consola se abre en su propia ventana; las exportaciones van a Descargas.
- Atajos: F5 recarga, Alt+← / Alt+→ atrás y adelante, Ctrl+W cierra la ventana.
- Abierta desde el menú, los mensajes van a `~/.cache/hangar/hangar.log`.
- Usa la GPU para dibujar (scroll a 60 fps; sin ella, por CPU, va a unos 35).
  Sin tarjeta gráfica, como en una VM, va por CPU. Si Chromium se cae con la
  GPU, `hangar-app` la vuelve a abrir sin ella y lo recuerda
  (`~/.cache/hangar/no-gpu`; bórralo para volver a probar). `HANGAR_NO_GPU=1`
  o `HANGAR_GPU=1` la fuerzan. `HANGAR_DEBUG=1` muestra cada página que carga.

## En el navegador

Sin instalar Qt: haz los pasos 1 a 4 y después

```bash
./run.sh            # http://127.0.0.1:8090
./run.sh --reload   # modo desarrollo: recarga al cambiar el código
```

### LXC

Los contenedores de `/var/lib/lxc` son de root. Para no ejecutar toda la app
como root, Hangar usa un script ayudante con un conjunto cerrado de acciones
y una regla de sudo solo para ese script:

```bash
sudo ./scripts/install-lxc-helper.sh
```

Instala `/usr/local/sbin/hangar-lxc-helper` y `/etc/sudoers.d/hangar`.
Vuelve a ejecutarlo cada vez que cambie `scripts/hangar-lxc-helper`.

Al modificar un contenedor, el ayudante guarda antes una copia del config en
`/var/lib/lxc/NOMBRE/config.hangar.bak`.

## Qué se aplica en caliente

| | CPU / memoria | Disco |
|---|---|---|
| KVM | en el próximo arranque si está encendida | al momento (luego amplía la partición dentro) |
| LXC | en caliente si está encendido (cgroups) | — |
| Docker | en caliente | — |

## Seguridad

Hangar controla el host con permisos equivalentes a root (grupos `libvirt`
y `docker`, más el ayudante LXC), así que:

- Escucha solo en `127.0.0.1`.
- Rechaza peticiones cuyo `Host` no sea `localhost`/`127.0.0.1` (DNS rebinding).
- Rechaza POST enviados desde otras webs (CSRF): una página maliciosa no puede
  mandar un formulario oculto a Hangar.
- Las notas en Markdown no ejecutan HTML incrustado.
- La consola web (websocket) exige que el `Origin` sea el propio Hangar: una
  web externa no puede abrir una shell en tus máquinas.

- Como aplicación de escritorio, además exige una clave aleatoria que solo
  conoce su ventana: otros usuarios del mismo equipo no pueden usarlo.

En modo navegador (`run.sh`) no tiene usuarios ni contraseña: en un equipo
compartido usa la aplicación de escritorio, y no lo expongas nunca a la red.

## Estructura

```
hangar/
  desktop.py           aplicación de escritorio (ventana Qt + servidor interno)
  main.py              rutas de máquinas y API JSON (FastAPI)
  common.py            plantillas, seguridad y utilidades compartidas
  console.py           consola web (websocket ↔ pseudo-terminal)
  networks.py          página de redes
  modules.py           acciones por módulo y exportación
  jobs.py              tareas en segundo plano
  db.py                módulo, descripción, notas y etiquetas (SQLite en data/)
  drivers/
    base.py            modelo Machine y la interfaz común de los drivers
    kvm.py             libvirt (qemu:///system)
    lxc.py             lxc-* a través del ayudante
    docker_driver.py   SDK oficial de Docker
  templates/           páginas HTML (Jinja2)
  static/              style.css y app.js
scripts/               ayudante LXC, acceso directo del menú y env.sh (prepara .venv)
hangar-app             abre la aplicación de escritorio
run.sh                 abre el servidor para usarlo en el navegador
```

Para añadir algo nuevo a las tres tecnologías: añade el método en
`drivers/base.py`, impleméntalo en cada driver y úsalo desde `main.py`.

## Módulos

Cada máquina puede pertenecer a un módulo del grado (ASO, BBDD, IMPWEB,
SERVICIOS, INFRAESTRUCTURA, PROYECTO INTEGRADO, SEGURIDAD). Se asigna desde el
desplegable de la portada, en «Descripción y notas» o al crearla. La lista
está en `MODULES` de `hangar/db.py`.

Por módulo puedes **encender o apagar todas** sus máquinas y **exportar** su
documentación en Markdown (máquinas, recursos, redes, IPs y tus notas), útil
para la memoria de una práctica.

## Consola

- **Consola** (máquina encendida): terminal con [xterm.js](https://xtermjs.org/)
  (incluido en `hangar/static/vendor`, funciona sin Internet).
  KVM usa la consola serie (`virsh console`), LXC `lxc-attach` y Docker
  `docker exec -it`.
- **Pantalla** (VMs con SPICE/VNC): abre `virt-viewer` en tu escritorio.

## Red

Pestaña **Red** de cada máquina: añadir y quitar interfaces y cambiar su IP.

| | Cómo se cambia la IP |
|---|---|
| KVM, red con DHCP | reserva DHCP en la red de libvirt (y renovación dentro si tiene agente) |
| KVM, red sin DHCP o bridge | dentro de la VM con qemu-guest-agent: NetworkManager, netplan, systemd-networkd o `/etc/network/interfaces` (persistente); `netsh` en Windows |
| LXC | `lxc.net.N.ipv4.address/gateway` en el config, al reiniciar |
| Docker | reconecta a la red con IP fija (solo redes creadas por ti con subred) |

La página **Redes** muestra todas las redes libvirt, bridges y redes Docker
con las máquinas e IPs de cada una, y permite crear redes libvirt (NAT o
aisladas, con o sin DHCP) y redes Docker.

## Crear máquinas

Botón **+ Nueva máquina**. La creación corre en segundo plano y abre una
página con el registro en directo (las tareas viven en memoria: si reinicias
Hangar se pierde el registro, no la máquina).

- **KVM**: usa `virt-install` (desde una ISO de cualquier pool activo; la VM
  arranca y la instalas con virt-manager/virt-viewer) o `virt-clone` (la VM
  origen debe estar apagada). Si osinfo no detecta la ISO, indica el sistema
  (p. ej. `debian13`, `win11`) para que elija los dispositivos adecuados.
- **LXC**: `lxc-create -t download`; la lista de imágenes se lee del servidor
  de linuxcontainers.org.
- **Docker**: si la imagen no está, se descarga antes de crear.

## Problemas frecuentes

**«No se puede conectar a Docker»**: Hangar indica la causa en el mensaje.

- *Tu usuario no tiene permiso*: añádelo al grupo `docker` y **cierra sesión
  y vuelve a entrar** (o reinicia); si no, el grupo no se aplica.

  ```bash
  sudo usermod -aG docker $USER
  ```
- *El servicio no está arrancado*: `sudo systemctl enable --now docker`
- *Docker no está instalado*: Hangar funciona igual con KVM y LXC; para
  usarlo, `sudo apt install docker.io`.
- Con **Docker Desktop** o **Docker rootless**, Hangar usa el mismo contexto
  que el comando `docker` (`docker context show`), así que no hay que
  configurar nada.

**«No se puede conectar a qemu:///system»**: instala libvirt
(`sudo apt install libvirt-daemon-system`) y añade tu usuario al grupo
`libvirt` (`sudo usermod -aG libvirt $USER`, y vuelve a iniciar sesión).

**«.venv/bin/uvicorn: No existe el fichero o el directorio»**: pasaba con
versiones antiguas de `run.sh` cuando la primera instalación fallaba a medias.
Actualiza (`git pull`) y vuelve a ejecutar `./run.sh`: ahora repara el entorno
solo, y si falta algo del sistema (`python3-venv`, `python3-libvirt`) te dice
qué instalar. También reinstala las dependencias cuando cambian tras un
`git pull`.

**«address already in use»**: ya hay un Hangar en marcha en el puerto 8090;
úsalo, páralo o arranca otro con `PORT=8091 ./run.sh`. La aplicación de
escritorio no tiene este problema: elige sola un puerto libre.

**La aplicación no se abre desde el menú**: mira `~/.cache/hangar/hangar.log`
o lánzala desde una terminal con `./hangar-app` para ver el error.

## API

- `GET /api/machines`: todas las máquinas con sus metadatos
- `GET /api/machines/{kind}/{id}`: ficha completa
- Documentación interactiva en `/docs`

## Hoja de ruta

- [x] Listar y ver la ficha de KVM, LXC y Docker
- [x] Descripción, etiquetas y notas en Markdown
- [x] Usuarios (`/etc/passwd`) de cada máquina
- [x] Arrancar, apagar, reiniciar, forzar apagado y reanudar si está pausada
- [x] Modificar CPU, memoria y arranque automático (KVM, LXC, Docker),
      ampliar discos KVM y política de reinicio Docker
- [x] Crear máquinas: KVM desde ISO o clonando, LXC desde las imágenes de
      `lxc-download`, Docker con puertos, volúmenes y variables
- [x] Módulos del grado, con acciones por módulo y exportación a Markdown
- [x] Consola web (terminal) y pantalla con virt-viewer
- [x] Añadir/quitar interfaces y cambiar IPs; página de redes
- [ ] Añadir/quitar discos
- [x] Borrar, con plan de lo que se borra/conserva y confirmación escribiendo
      el nombre (KVM no borra ISOs ni discos que use otra VM)
- [x] Snapshots: crear, listar, volver y borrar (KVM internos, con memoria si
      está encendida; LXC con `lxc-snapshot`, contenedor apagado)
- [ ] Métricas (gráficas de CPU/RAM)
